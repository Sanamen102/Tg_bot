"""Резервная копия фото на USB-диск: что о ней знает бот.

Саму копию делает хост — server/sbin/photo-backup.py по таймеру каждую ночь.
Бот только читает файл состояния, который тот оставляет в data/, и по нему
предупреждает о сбоях и показывает бэкап в сводках.
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from app.config import settings
from app.formatting import esc, human_bytes

log = logging.getLogger(__name__)


def read_status() -> dict | None:
    """None — бэкап ещё ни разу не запускался (или функция не используется)."""
    path = Path(settings.photo_backup_status)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("Не читается %s: %s", path, e)
        return None


def _ts(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _when(dt: datetime) -> str:
    # Хост пишет время в UTC, показываем по местному (TZ контейнера бота)
    return dt.astimezone().strftime("%d.%m %H:%M")


def problem(st: dict, now: datetime | None = None) -> str | None:
    """Текст алерта или None, если с бэкапом всё в порядке.

    Отключённый диск — не повод будить в первую же ночь: его могли взять на
    время. Тревожно, когда копии нет дольше PHOTO_BACKUP_MAX_AGE_DAYS. Любая
    другая ошибка (диск с ошибками ФС, кончилось место, сбой rsync) — сразу.
    """
    now = now or datetime.now().astimezone()
    error = st.get("error") or ""
    last_ok = _ts(st.get("last_ok"))
    if not st.get("ok") and error and "не подключён" not in error:
        return f"🗄 Бэкап фото на USB-диск не прошёл: {esc(error)}"
    if not st.get("ok") and last_ok is None:
        # Отсчитывать «давно ли» не от чего: время попытки обновляется каждую
        # ночь, и тревога не пришла бы никогда
        return f"🗄 Бэкап фото на USB-диск ещё ни разу не прошёл: {esc(error or 'ошибка')}"
    max_age = timedelta(days=settings.photo_backup_max_age_days)
    if last_ok and now - last_ok > max_age:
        reason = f" Последняя попытка: {esc(error)}." if error else ""
        return (
            f"🗄 Бэкап фото на USB-диск не делался с {_when(last_ok)} "
            f"({(now - last_ok).days} дн.).{reason}"
        )
    return None


def summary_line(st: dict | None) -> str:
    title = "🗄 <b>Бэкап фото:</b>"
    if not st:
        return f"{title} ещё не делался."
    last_ok = _ts(st.get("last_ok"))
    if not st.get("ok"):
        when = f"последний удачный {_when(last_ok)}" if last_ok else "удачных ещё не было"
        return f"{title} ⚠️ {esc(st.get('error') or 'ошибка')} ({when})."
    parts = [f"{_when(last_ok)}" if last_ok else "сделан"]
    if st.get("files"):
        count = f"{st['files']:,}".replace(",", " ")
        parts.append(f"{count} файлов, {human_bytes(st.get('total_bytes', 0))}")
    if st.get("copied"):
        parts.append(f"новых {st['copied']}")
    if st.get("disk_free"):
        parts.append(f"на диске свободно {human_bytes(st['disk_free'])}")
    return f"{title} " + " · ".join(parts)
