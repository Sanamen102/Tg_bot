#!/usr/bin/env python3
"""Локальное зеркало конфигов VPN на домашнем сервере.

Зачем: раздача конфигов и управление сейчас держатся на боте, а бот ходит в
Telegram через сам VPN. Упал VPN — упал и канал, по которому можно было бы
раздать новый конфиг. Это зеркало от Telegram не зависит вообще.

Что делает: тянет с VPS список пользователей и содержимое их подписок,
складывает рядом статическую страницу с ссылками и QR-кодами, плюс локальные
копии подписок, которые домашние устройства могут использовать напрямую.

Главное свойство: если VPS недоступен, скрипт НИЧЕГО не трогает и выходит с
ошибкой. Предыдущая копия остаётся на месте — иначе отказ сервера уносил бы
с собой и резервные конфиги, ровно тогда, когда они нужнее всего.

Запускается по таймеру systemd, результат отдаёт nginx по локальной сети.
"""

import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone

VPS_HOST = os.environ.get("VPN_MIRROR_HOST", "147.45.72.72")
VPS_USER = os.environ.get("VPN_MIRROR_USER", "root")
SSH_KEY = os.environ.get("VPN_MIRROR_KEY", "/home/san/Tg_bot/ssh/id_ed25519_vpn")
OUT_DIR = os.environ.get("VPN_MIRROR_OUT", "/home/san/vpn-mirror/www")
# Постоянный адрес раздачи. Живёт на домашнем сервере, поэтому переживает
# любую смену VPN-сервера — именно он и выдаётся людям.
PUBLIC_URL = os.environ.get("VPN_MIRROR_PUBLIC_URL", "http://192.168.101.7:8090")
FETCH_TIMEOUT = 20


def fail(msg):
    print("ОТМЕНА: %s" % msg, file=sys.stderr)
    print("Предыдущая копия оставлена нетронутой.", file=sys.stderr)
    sys.exit(1)


def fetch_users():
    """Список пользователей через forced command на VPS."""
    cmd = [
        "ssh", "-i", SSH_KEY, "-n",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
        "-o", "StrictHostKeyChecking=accept-new",
        "%s@%s" % (VPS_USER, VPS_HOST), "users",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        fail("VPS не ответил за 60 секунд")
    if out.returncode != 0:
        fail("ssh вернул код %s: %s" % (out.returncode, (out.stderr or "").strip()[:200]))
    try:
        data = json.loads((out.stdout or "").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        fail("VPS ответил неразборчиво")
    if not data.get("ok"):
        fail(data.get("error", "неизвестная ошибка на VPS"))
    users = data.get("users") or []
    if not users:
        fail("VPS вернул пустой список пользователей")
    return users


def fetch_sub(url):
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as r:
            body = r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        fail("не удалось забрать подписку %s: %s" % (url, type(e).__name__))
    if "type: mieru" not in body:
        fail("подписка %s пришла без узлов mieru — похоже, битая" % url)
    return body


def qr_png(data):
    import segno
    import io
    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="png", scale=6, border=3,
                                     dark="#000000", light="#FFFFFF")
    return buf.getvalue()


def ago(ts):
    if not ts:
        return "ни разу не заходил"
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return "?"
    d = (datetime.now(timezone.utc) - dt).total_seconds()
    if d < 90:
        return "только что"
    if d < 3600:
        return "%d мин назад" % (d // 60)
    if d < 86400:
        return "%d ч назад" % (d // 3600)
    return "%d дн назад" % (d // 86400)


def mb(n):
    n = int(n or 0)
    if n < 1024 ** 3:
        return "%.0f МБ" % (n / 1048576)
    return "%.1f ГБ" % (n / 1073741824)


PAGE_HEAD = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Конфиги VPN</title>
<style>
 body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:900px;
      margin:0 auto;padding:16px;line-height:1.5;background:#faf9f7;color:#1a1a1a}
 h1{font-size:22px;font-weight:500;margin:0 0 4px}
 .sub{color:#666;font-size:14px;margin-bottom:20px}
 .u{background:#fff;border:1px solid #e3e0d8;border-radius:10px;padding:14px;margin-bottom:14px;
    display:flex;gap:14px;align-items:flex-start;flex-wrap:wrap}
 .qrs{display:flex;gap:10px;flex-shrink:0}
 .qr{text-align:center;font-size:11px;color:#666;width:118px}
 .qr img{display:block;margin-bottom:3px}
 .qr b{display:block;font-weight:500;color:#1a1a1a;font-size:12px}
 .u h2{font-size:17px;font-weight:500;margin:0 0 6px}
 .meta{color:#666;font-size:13px;margin-bottom:10px}
 .l{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;background:#f4f2ee;
    border:1px solid #e3e0d8;border-radius:6px;padding:7px 9px;margin:5px 0;
    word-break:break-all;overflow-wrap:anywhere}
 .lab{font-size:12px;color:#666;margin-top:8px}
 img{border-radius:6px;flex-shrink:0}
 .col{flex:1;min-width:260px}
 .warn{background:#fff6e6;border:1px solid #f0d9a8;border-radius:8px;padding:10px 12px;
       font-size:14px;margin-bottom:18px}
 footer{color:#888;font-size:12px;margin-top:22px;border-top:1px solid #e3e0d8;padding-top:12px}
 @media(prefers-color-scheme:dark){
  body{background:#1c1b19;color:#eee} .u{background:#252320;border-color:#3a3733}
  .l{background:#1c1b19;border-color:#3a3733;color:#ddd} .sub,.meta,.lab,footer,.qr{color:#999}
  .qr b{color:#eee} .warn{background:#2e2716;border-color:#4d4122}}
</style>
"""


def render(users, generated):
    parts = [PAGE_HEAD,
             "<h1>Конфиги VPN</h1>",
             '<div class="sub">Резервная раздача с домашнего сервера. '
             'Не зависит ни от Telegram, ни от доступности VPS.</div>',
             '<div class="warn">Основная ссылка раздаётся с домашнего сервера и '
             'работает откуда угодно. Она не изменится при смене VPN-сервера — '
             'выдавайте людям именно её.</div>']
    for u in users:
        name = html.escape(u["name"])
        token = u["sub"].rsplit("/", 1)[1] if u.get("sub") else ""
        local = "%s/sub/%s" % (PUBLIC_URL, token) if token else ""
        parts.append('<div class="u">')
        parts.append('<div class="qrs">')
        if local:
            parts.append('<div class="qr"><img src="qr/%s-local.png" width="118" height="118" '
                         'alt="QR основной подписки %s"><b>Основная</b>постоянный адрес</div>'
                         % (name, name))
        if u.get("sub"):
            parts.append('<div class="qr"><img src="qr/%s-remote.png" width="118" height="118" '
                         'alt="QR запасной подписки %s"><b>Запасная</b>напрямую с VPS</div>'
                         % (name, name))
        parts.append("</div>")
        parts.append('<div class="col">')
        parts.append("<h2>%s</h2>" % name)
        parts.append('<div class="meta">%s &middot; скачано %s &middot; отдано %s</div>'
                     % (ago(u.get("lastActive")), mb(u.get("down")), mb(u.get("up"))))
        if local:
            parts.append('<div class="lab">Основная подписка — её и выдавайте</div>')
            parts.append('<div class="l">%s</div>' % html.escape(local))
        if u.get("sub"):
            parts.append('<div class="lab">Запасная, напрямую с VPS</div>')
            parts.append('<div class="l">%s</div>' % html.escape(u["sub"]))
        if u.get("link"):
            parts.append('<div class="lab">Прямая ссылка (запасной вариант)</div>')
            parts.append('<div class="l">%s</div>' % html.escape(u["link"]))
        parts.append("</div></div>")
    parts.append("<footer>Обновлено %s. Если время давнее — синхронизация с VPS "
                 "не проходит, но показанные конфиги остаются рабочими.</footer>"
                 % generated)
    return "\n".join(parts)


def main():
    users = fetch_users()
    subs = {}
    for u in users:
        if u.get("sub"):
            subs[u["sub"].rsplit("/", 1)[1]] = fetch_sub(u["sub"])

    # Всё собрано успешно — только теперь трогаем то, что отдаётся наружу.
    #
    # Файлы подменяем по одному через rename, а сам каталог НЕ пересоздаём:
    # docker привязывает bind-mount к inode при старте контейнера, и подмена
    # каталога целиком оставила бы nginx смотреть в удалённый inode. Отдача
    # молча превращалась бы в 404 после первой же синхронизации.
    for sub in ("", "qr", "sub"):
        os.makedirs(os.path.join(OUT_DIR, sub), exist_ok=True)
        os.chmod(os.path.join(OUT_DIR, sub), 0o755)

    def put(rel, data):
        path = os.path.join(OUT_DIR, rel)
        mode = "wb" if isinstance(data, bytes) else "w"
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
        try:
            with os.fdopen(fd, mode) as f:
                f.write(data)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except Exception:
            os.unlink(tmp)
            raise

    written = {"index.html"}
    for token, body in subs.items():
        put(os.path.join("sub", token), body)
        written.add(os.path.join("sub", token))
    # Два QR на человека: локальный работает только из дома, внешний — откуда
    # угодно, пока жив VPS. Кому какой давать, зависит от того, где он сидит,
    # поэтому показываем оба и подписываем.
    for u in users:
        token = u["sub"].rsplit("/", 1)[1] if u.get("sub") else ""
        targets = {}
        if token:
            targets["%s-local" % u["name"]] = "%s/sub/%s" % (PUBLIC_URL, token)
        if u.get("sub"):
            targets["%s-remote" % u["name"]] = u["sub"]
        if not targets and u.get("link"):
            targets["%s-local" % u["name"]] = u["link"]
        for stem, payload in targets.items():
            rel = os.path.join("qr", "%s.png" % stem)
            put(rel, qr_png(payload))
            written.add(rel)
    put("index.html", render(users, datetime.now().strftime("%d.%m.%Y %H:%M")))

    # Прибираем за удалёнными пользователями, чтобы отозванный конфиг не
    # продолжал раздаваться с зеркала
    stale = 0
    for sub in ("qr", "sub"):
        for name in os.listdir(os.path.join(OUT_DIR, sub)):
            rel = os.path.join(sub, name)
            if rel not in written and not name.startswith(".tmp-"):
                os.unlink(os.path.join(OUT_DIR, rel))
                stale += 1

    print("готово: %d пользователей, %d подписок%s"
          % (len(users), len(subs), ", убрано лишних файлов: %d" % stale if stale else ""))


if __name__ == "__main__":
    main()
