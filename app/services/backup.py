"""Сбор конфигов сервера в tar.gz для отправки в Telegram.

Бот работает в контейнере, но корень хоста примонтирован read-only в
/host/root — через него и читаем. Берём только конфиг-файлы (по именам и
расширениям), большие файлы и служебные каталоги пропускаем, поэтому архив
получается лёгким (мегабайты, а не гигабайты медиатеки).

Внимание: архив содержит секреты (.env, ключи) — он и должен их содержать,
в этом смысл бэкапа. Отправляется только в приватный чат владельца.
"""

import io
import logging
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

HOST_ROOT = Path("/host/root")

INCLUDE_NAMES = {".env", "config", "authorized_keys"}
INCLUDE_SUFFIXES = {
    ".yml", ".yaml", ".env", ".conf", ".json", ".sh",
    ".service", ".timer", ".txt", ".list", ".xml", ".ini", ".toml",
}
INCLUDE_PREFIXES = ("id_",)  # ssh-ключи

EXCLUDE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "media", "library", "upload", "postgres", "pgdata",
    "cache", "model-cache", "transcodes", "metadata",
}

MAX_FILE_SIZE = 2 * 1024 * 1024        # файлы больше 2 МБ — не конфиги
MAX_TOTAL_SIZE = 40 * 1024 * 1024      # лимит Telegram на документ — 50 МБ


@dataclass
class BackupResult:
    data: bytes
    files: int
    raw_size: int          # суммарный размер файлов до сжатия
    skipped_big: int       # пропущено из-за MAX_FILE_SIZE
    truncated: bool        # упёрлись в MAX_TOTAL_SIZE


def _wanted(name: str) -> bool:
    if name in INCLUDE_NAMES or name.startswith(INCLUDE_PREFIXES):
        return True
    return Path(name).suffix.lower() in INCLUDE_SUFFIXES


def build_archive() -> BackupResult:
    roots = settings.backup_path_list
    if not roots:
        raise ServiceError(
            "Бэкап не настроен: задайте BACKUP_PATHS в .env "
            "(каталоги хоста через запятую, например /home/san,/opt/zapret)."
        )
    base = HOST_ROOT if HOST_ROOT.exists() else Path("/")

    class _ArchiveFull(Exception):
        pass

    buf = io.BytesIO()
    files = 0
    raw_size = 0
    skipped_big = 0
    truncated = False

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        try:
            for root in roots:
                top = base / root.lstrip("/")
                if not top.exists():
                    log.warning("Бэкап: каталог %s не найден, пропускаю", root)
                    continue
                for dirpath, dirnames, filenames in os.walk(top):
                    dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
                    for filename in filenames:
                        if not _wanted(filename):
                            continue
                        path = Path(dirpath) / filename
                        try:
                            size = path.stat().st_size
                            if size > MAX_FILE_SIZE:
                                skipped_big += 1
                                continue
                            if raw_size + size > MAX_TOTAL_SIZE:
                                raise _ArchiveFull
                            # В архиве — путь как на хосте, без /host/root
                            try:
                                arcname = path.relative_to(base).as_posix()
                            except ValueError:
                                arcname = path.relative_to(path.anchor).as_posix()
                            tar.add(path, arcname=arcname, recursive=False)
                            files += 1
                            raw_size += size
                        except OSError as e:
                            log.warning("Бэкап: не удалось прочитать %s: %s", path, e)
        except _ArchiveFull:
            truncated = True

    if files == 0:
        raise ServiceError(
            "Бэкап пуст: в указанных BACKUP_PATHS не нашлось ни одного конфиг-файла."
        )
    return BackupResult(
        data=buf.getvalue(),
        files=files,
        raw_size=raw_size,
        skipped_big=skipped_big,
        truncated=truncated,
    )
