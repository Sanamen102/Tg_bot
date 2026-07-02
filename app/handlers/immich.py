"""Команды Immich: /immich_status, /memory, /memory_today."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app import actions
from app.formatting import esc, human_bytes
from app.services.errors import ServiceError
from app.services.immich import ImmichClient

router = Router(name="immich")


@router.message(Command("immich_status"))
async def cmd_immich_status(message: Message) -> None:
    try:
        immich = ImmichClient()
        if not await immich.ping():
            await message.answer("📸 Immich: ❌ не отвечает на ping.")
            return
        version = await immich.version()
        lines = [f"📸 <b>Immich</b>: ✅ доступен (v{esc(version)})"]
        stats = await immich.statistics()
        if stats:
            lines.append(f"🖼 Фото: {stats.get('photos', '?')}")
            lines.append(f"🎥 Видео: {stats.get('videos', '?')}")
            usage = stats.get("usage")
            if usage:
                lines.append(f"💾 Занято: {human_bytes(usage)}")
        else:
            lines.append("ℹ️ Статистика недоступна (для неё нужен API-ключ администратора).")
        await message.answer("\n".join(lines))
    except ServiceError as e:
        await message.answer(f"⚠️ {esc(e.user_message)}")


@router.message(Command("memory"))
async def cmd_memory(message: Message) -> None:
    await message.answer("🎲 Ищу случайное фото…")
    try:
        await actions.send_random_asset(message.bot, message.chat.id)
    except ServiceError as e:
        await message.answer(f"⚠️ {esc(e.user_message)}")


@router.message(Command("memory_today"))
async def cmd_memory_today(message: Message) -> None:
    await message.answer("🕰 Ищу фото, сделанные в этот день в прошлые годы…")
    try:
        await actions.send_memories_today(message.bot, message.chat.id)
    except ServiceError as e:
        await message.answer(f"⚠️ {esc(e.user_message)}")
