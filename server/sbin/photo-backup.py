#!/usr/bin/env python3
"""Резервная копия фотоархива Immich на USB-диск.

Запускается таймером photo-backup.timer каждую ночь. Диск монтируется только
на время копирования и сразу отключается: пока он не смонтирован, ни ошибка,
ни шифровальщик на сервере не могут испортить резервную копию.

Что копируется (из UPLOAD_LOCATION Immich):
  library/  — оригиналы по шаблону хранения: <пользователь>/<год>/<год-месяц>/<имя>
  upload/   — то, что Immich ещё не разложил по папкам (обычно пусто)
  profile/  — аватарки
  backups/  — ежедневные дампы базы Immich: альбомы, лица, описания
thumbs/ и encoded-video/ не копируются — Immich пересоздаёт их сам.

Удалённое из Immich не стирается с USB-диска, а переезжает в
Immich/_удалённые/<дата>/ — случайно удалённое фото можно вернуть.
Дампы базы — исключение: Immich сам хранит 14 последних, копия повторяет.

Итог пишется в data/photo-backup.json бота: бот предупредит, если бэкап
падает или давно не делался (USB-диск отключили), и покажет его в сводке.

Диск — exFAT, чтобы его можно было просто подключить к любому компьютеру.
Отсюда -rt вместо -a (exFAT не хранит владельцев и права) и --modify-window.
"""
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

DISK_UUID = os.environ.get("PHOTO_BACKUP_UUID", "8EBD-4906")
MNT = "/mnt/photo-backup"
SRC = "/mnt/storage/immich-app/library"
DEST = os.path.join(MNT, "Immich")
STATUS = os.environ.get("PHOTO_BACKUP_STATUS", "/home/san/Tg_bot/data/photo-backup.json")
LOCK = "/run/photo-backup.lock"
MEDIA_DIRS = ("library", "upload", "profile")
DELETED = "_удалённые"
MIN_FREE = 20 * 1024 ** 3

README = """\
Резервная копия фотоархива Immich с домашнего сервера.
Обновляется сама каждую ночь, пока диск подключён к серверу.

Где фото:
  Immich/library/admin/<год>/<год-месяц>/  — все фото и видео, исходные имена файлов
  Immich/library/<длинный id>/             — фото второго пользователя Immich
  Immich/upload/                           — то, что Immich ещё не разложил (обычно пусто)
  Immich/backups/                          — копии базы Immich (альбомы, лица, описания)
  Immich/_удалённые/<дата>/                — удалённое из Immich. Само НЕ стирается,
                                             чистить вручную, когда понадобится место.

Как поднять Immich из этой копии:
  1. Установить Immich той же версии (docker compose).
  2. Скопировать папку Immich сюда как UPLOAD_LOCATION (в ней library, upload,
     profile, backups) — папку _удалённые можно не брать.
  3. Восстановить базу из самого свежего файла в backups/ — инструкция:
     https://immich.app/docs/administration/backup-and-restore
  Превью и пережатые видео Immich пересоздаст сам.

Папку «Фото» в корне диска этот бэкап не трогает.
"""


class BackupError(Exception):
    pass


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def run(cmd, timeout=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def load_status():
    try:
        with open(STATUS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_status(st):
    os.makedirs(os.path.dirname(STATUS), exist_ok=True)
    tmp = STATUS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATUS)


def stat_num(text, pattern):
    m = re.search(pattern, text, re.M)
    return int(m.group(1).replace(",", "")) if m else 0


def rsync(args):
    """rsync с разбором --stats. Код 24 — файлы исчезли во время копирования:
    Immich как раз раскладывал их по папкам, следующий прогон их подберёт."""
    out = run(["rsync", "-rt", "--modify-window=2", "--stats", *args])
    if out.returncode not in (0, 24):
        tail = (out.stderr or out.stdout).strip().splitlines()[-3:]
        raise BackupError("rsync завершился с кодом %d: %s" % (out.returncode, " | ".join(tail)[:400]))
    return out.stdout, out.returncode == 24


def mount(dev):
    if os.path.ismount(MNT):
        src = run(["findmnt", "-no", "SOURCE", MNT]).stdout.strip()
        if src != dev:
            raise BackupError("в %s смонтировано что-то другое (%s)" % (MNT, src))
        return  # остался от прерванного прогона — используем
    # Проверка без исправлений: если диск выдернули посреди записи, лучше
    # узнать и проверить его на ПК, чем писать поверх повреждённой ФС
    fsck = run(["fsck.exfat", "-n", dev], timeout=3600)
    if fsck.returncode != 0:
        msg = (fsck.stdout + fsck.stderr).strip().splitlines()[-1:] or ["?"]
        raise BackupError("файловая система USB-диска с ошибками (%s) — писать не стал. "
                          "Проверьте диск на ПК: chkdsk /f" % msg[0][:200])
    os.makedirs(MNT, exist_ok=True)
    out = run(["mount", "-t", "exfat", "-o", "rw,noatime,nodev,nosuid,noexec", dev, MNT])
    if out.returncode != 0:
        raise BackupError("не удалось смонтировать USB-диск: %s" % out.stderr.strip()[:200])


def unmount():
    run(["sync"])
    for _ in range(5):
        if not os.path.ismount(MNT) or run(["umount", MNT]).returncode == 0:
            return True
        time.sleep(5)
    return False


def backup():
    dev = run(["blkid", "-U", DISK_UUID]).stdout.strip()
    if not dev:
        raise BackupError("USB-диск не подключён (раздел %s не найден)" % DISK_UUID)
    if not os.path.isdir(os.path.join(SRC, "library")):
        raise BackupError("нет %s/library — библиотека Immich не на месте, копировать нечего" % SRC)
    mount(dev)
    try:
        usage = shutil.disk_usage(MNT)
        if usage.free < MIN_FREE:
            raise BackupError("на USB-диске осталось %.0f ГБ — нужно освободить место "
                              "(например, почистить Immich/_удалённые)" % (usage.free / 1024 ** 3))
        os.makedirs(DEST, exist_ok=True)
        readme = os.path.join(DEST, "README.txt")
        try:
            with open(readme, encoding="utf-8") as f:
                same = f.read() == README
        except OSError:
            same = False
        if not same:
            with open(readme, "w", encoding="utf-8") as f:
                f.write(README)

        sources = [os.path.join(SRC, d) for d in MEDIA_DIRS if os.path.isdir(os.path.join(SRC, d))]
        day = datetime.now().strftime("%Y-%m-%d")
        media, vanished = rsync([
            "--delete", "--backup", "--backup-dir=%s/%s/%s" % (DEST, DELETED, day),
            *sources, DEST + "/",
        ])
        db, vanished_db = rsync(["--delete", os.path.join(SRC, "backups"), DEST + "/"])
        usage = shutil.disk_usage(MNT)
        warnings = []
        if vanished or vanished_db:
            warnings.append("часть файлов Immich переложил во время копирования — "
                            "они попадут в следующий прогон")
        return {
            "files": stat_num(media, r"^Number of files: [\d,]+ \(reg: ([\d,]+)"),
            "copied": stat_num(media, r"^Number of regular files transferred: ([\d,]+)"),
            "copied_bytes": stat_num(media, r"^Total transferred file size: ([\d,]+)"),
            "moved_to_deleted": stat_num(media, r"^Number of deleted files: ([\d,]+)"),
            "total_bytes": stat_num(media, r"^Total file size: ([\d,]+)"),
            "db_copied": stat_num(db, r"^Number of regular files transferred: ([\d,]+)"),
            "disk_free": usage.free,
            "disk_total": usage.total,
            "warnings": warnings,
        }
    finally:
        if not unmount():
            print("ВНИМАНИЕ: не удалось отмонтировать %s" % MNT, file=sys.stderr)


def main():
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("бэкап уже идёт")
        return 0
    prev = load_status()
    started = time.time()
    st = {"started": now_iso(), "ok": False, "last_ok": prev.get("last_ok")}
    try:
        st.update(backup())
        st["ok"] = True
    except BackupError as e:
        st["error"] = str(e)
    except Exception as e:  # чтобы бот узнал и о непредвиденном
        st["error"] = "%s: %s" % (type(e).__name__, e)
    st["finished"] = now_iso()
    st["duration_s"] = int(time.time() - started)
    if st["ok"]:
        st["last_ok"] = st["finished"]
    save_status(st)
    if st["ok"]:
        print("готово: %d файлов, скопировано %d (%.1f ГБ), в _удалённые %d, за %d с, свободно %.0f ГБ"
              % (st["files"], st["copied"], st["copied_bytes"] / 1024 ** 3, st["moved_to_deleted"],
                 st["duration_s"], st["disk_free"] / 1024 ** 3))
        return 0
    print("ОШИБКА: %s" % st["error"], file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
