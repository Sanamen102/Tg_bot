"""Приём книг файлом: прислал .epub/.pdf в чат — попало в библиотеку Kavita.

У Kavita нет загрузки через веб — она следит за папкой. Бот закрывает этот
пробел: книгу можно закинуть с любого устройства, в том числе переслать
из другого чата.
"""

import logging
import re
from pathlib import Path

from aiogram import F, Router
from aiogram.types import Message

from app.config import settings
from app.formatting import esc, human_bytes

log = logging.getLogger(__name__)
router = Router(name="books")

BOOK_EXTENSIONS = {
    ".epub", ".fb2", ".pdf", ".mobi", ".azw3", ".azw",
    ".djvu", ".cbz", ".cbr", ".txt",
}

# Telegram не даёт ботам скачивать файлы больше 20 МБ
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024


def _safe_name(name: str) -> str:
    """Убираем всё, чем можно навредить файловой системе."""
    name = Path(name).name  # отсекаем любые пути
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name.strip() or "book"


def _unique_path(directory: Path, name: str) -> Path:
    path = directory / name
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(2, 100):
        candidate = directory / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem} ({Path(name).stat().st_mtime:.0f}){suffix}"


@router.message(F.document)
async def on_document(message: Message) -> None:
    doc = message.document
    ext = Path(doc.file_name or "").suffix.lower()
    if ext not in BOOK_EXTENSIONS:
        return  # не книга — молча пропускаем

    if not settings.books_dir:
        await message.answer(
            "📚 Библиотека книг не настроена: задайте BOOKS_DIR в .env."
        )
        return

    if doc.file_size and doc.file_size > TG_DOWNLOAD_LIMIT:
        await message.answer(
            f"⚠️ Файл {human_bytes(doc.file_size)} — Telegram не позволяет ботам "
            "скачивать файлы больше 20 МБ.\nПоложите его в папку книг вручную "
            "(например, через торрент или по SSH)."
        )
        return

    directory = Path(settings.books_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("Не удалось создать папку книг: %s", e)
        await message.answer("⚠️ Папка библиотеки недоступна для записи.")
        return

    waiting = await message.answer("📚 Сохраняю в библиотеку…")
    path = _unique_path(directory, _safe_name(doc.file_name))
    try:
        await message.bot.download(doc, destination=path)
    except Exception:
        log.exception("Не удалось сохранить книгу")
        await waiting.edit_text("⚠️ Не удалось сохранить файл, подробности в логах бота.")
        return

    size = path.stat().st_size
    await waiting.edit_text(
        f"✅ Книга в библиотеке: <b>{esc(path.name)}</b> ({human_bytes(size)})\n"
        "Появится в Kavita после сканирования папки."
    )
