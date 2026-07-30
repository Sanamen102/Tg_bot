"""Скачивание видео по ссылке: кинул ссылку в чат — выбрал кнопкой, что делать.

Ссылка не влезает в callback_data (лимит 64 байта), поэтому храним её
в памяти по короткому токену — как сделано для magnet-ссылок.
"""

import logging
import secrets
from collections import OrderedDict
from pathlib import Path

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.config import settings
from app.formatting import esc, human_bytes, human_duration
from app.services import ytdl
from app.services.errors import ServiceError

log = logging.getLogger(__name__)
router = Router(name="ytdl")

_pending: OrderedDict[str, str] = OrderedDict()
_PENDING_MAX = 20


@router.message(F.text.func(lambda t: bool(ytdl.find_url(t or ""))))
async def on_link(message: Message) -> None:
    url = ytdl.find_url(message.text)
    if url is None:
        return

    waiting = await message.answer("🔍 Смотрю, что по ссылке…")
    try:
        info = await ytdl.probe(url)
    except ServiceError as e:
        await waiting.edit_text(f"⚠️ {esc(e.user_message)}")
        return

    if info.is_live:
        await waiting.edit_text("⚠️ Это прямой эфир — скачать его нельзя.")
        return

    token = secrets.token_urlsafe(6)
    _pending[token] = url
    while len(_pending) > _PENDING_MAX:
        _pending.popitem(last=False)

    rows = [
        [InlineKeyboardButton(text="📱 Видео в чат", callback_data=f"yt:{token}:chat")],
        [InlineKeyboardButton(text="🎵 Только звук", callback_data=f"yt:{token}:audio")],
    ]
    if settings.ytdl_library_dir:
        rows.insert(
            1, [InlineKeyboardButton(text="📺 В медиатеку", callback_data=f"yt:{token}:library")]
        )
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=f"yt:{token}:x")])

    lines = [f"🎬 <b>{esc(info.title)}</b>"]
    details = []
    if info.uploader:
        details.append(esc(info.uploader))
    if info.duration:
        details.append(human_duration(info.duration))
    if details:
        lines.append(" · ".join(details))
    lines.append("\nЧто сделать?")

    await waiting.edit_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@router.callback_query(F.data.startswith("yt:"))
async def on_choice(callback: CallbackQuery) -> None:
    try:
        _, token, mode = callback.data.split(":")
    except ValueError:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    url = _pending.get(token)
    if url is None:
        await callback.answer("Ссылка устарела — пришлите её заново.", show_alert=True)
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
        return

    if mode == "x":
        _pending.pop(token, None)
        if callback.message:
            await callback.message.edit_text("Отменено.")
        await callback.answer()
        return

    _pending.pop(token, None)
    await callback.answer()
    msg = callback.message
    if msg is None:
        return

    labels = {"chat": "видео", "audio": "звук", "library": "видео в медиатеку"}
    await msg.edit_text(f"⏳ Скачиваю {labels.get(mode, '')}… это может занять пару минут.")

    dest = Path(settings.ytdl_library_dir) if mode == "library" else None
    try:
        result = await ytdl.download(url, mode, dest)
    except ServiceError as e:
        await msg.edit_text(f"⚠️ {esc(e.user_message)}")
        return
    except Exception:
        log.exception("Ошибка скачивания %s", mode)
        await msg.edit_text("⚠️ Внутренняя ошибка при скачивании, подробности в логах бота.")
        return

    if mode == "library":
        await msg.edit_text(
            f"✅ Готово: <b>{esc(result.title)}</b>\n"
            f"📺 Сохранено в медиатеку ({human_bytes(result.size)}).\n"
            f"Файл: <code>{esc(result.path.name)}</code>"
        )
        return

    await msg.edit_text(f"📤 Отправляю ({human_bytes(result.size)})…")
    try:
        file = FSInputFile(result.path)
        if mode == "audio":
            await msg.answer_audio(file, title=result.title[:64])
        else:
            await msg.answer_video(
                file, caption=esc(result.title)[:1024], supports_streaming=True
            )
        await msg.delete()
    except Exception:
        log.exception("Не удалось отправить файл в Telegram")
        await msg.edit_text(
            "⚠️ Скачалось, но Telegram не принял файл "
            "(возможно, слишком большой). Попробуйте «В медиатеку»."
        )
    finally:
        ytdl.cleanup(result.path)
