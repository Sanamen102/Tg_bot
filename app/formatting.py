"""Хелперы форматирования сообщений."""

import html
from datetime import datetime

MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def esc(text: str) -> str:
    """Экранирование для HTML parse_mode."""
    return html.escape(str(text))


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024 or unit == "PB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    parts.append(f"{minutes}м")
    return " ".join(parts)


def progress_bar(percent: float, width: int = 10) -> str:
    filled = round(percent / 100 * width)
    filled = max(0, min(width, filled))
    return "▰" * filled + "▱" * (width - filled)


def ru_date(dt: datetime) -> str:
    return f"{dt.day} {MONTHS_RU[dt.month - 1]} {dt.year}"


def ru_years_ago(years: int) -> str:
    if years % 10 == 1 and years % 100 != 11:
        word = "год"
    elif years % 10 in (2, 3, 4) and years % 100 not in (12, 13, 14):
        word = "года"
    else:
        word = "лет"
    return f"{years} {word} назад"
