"""Команда /backup — прислать архив конфигов сервера в чат."""

import asyncio
from datetime import date

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from app.formatting import esc, human_bytes
from app.services import backup as backup_service
from app.services.errors import ServiceError

router = Router(name="backup")


async def send_backup(bot: Bot, chat_id: int, prefix: str = "") -> None:
    result = await asyncio.to_thread(backup_service.build_archive)
    caption = (
        f"{prefix}🗄 Бэкап конфигов: {result.files} файлов, "
        f"{human_bytes(result.raw_size)} (в архиве {human_bytes(len(result.data))})"
    )
    if result.skipped_big:
        caption += f"\nПропущено больших файлов: {result.skipped_big}"
    if result.truncated:
        caption += "\n⚠️ Архив упёрся в лимит 40 МБ и был обрезан!"
    document = BufferedInputFile(
        result.data, filename=f"homelab-config-{date.today():%Y-%m-%d}.tar.gz"
    )
    await bot.send_document(chat_id, document, caption=caption)


@router.message(Command("backup"))
async def cmd_backup(message: Message) -> None:
    waiting = await message.answer("🗄 Собираю конфиги…")
    try:
        await send_backup(message.bot, message.chat.id)
        await waiting.delete()
    except ServiceError as e:
        await waiting.edit_text(f"⚠️ {esc(e.user_message)}")
