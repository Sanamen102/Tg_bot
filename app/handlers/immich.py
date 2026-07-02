"""Команды Immich: /immich_status, /memory, /memory_today, /day."""

import re
from datetime import date, datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from app import actions
from app.formatting import esc, human_bytes
from app.services.errors import ServiceError
from app.services.immich import ImmichClient

router = Router(name="immich")


def parse_day(text: str) -> date | None:
    """Понимает 2024-07-02, 02.07.2024, 02.07.24 и 02.07 (текущий год)."""
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    if re.fullmatch(r"\d{1,2}\.\d{1,2}", text):
        try:
            day, month = map(int, text.split("."))
            return date(date.today().year, month, day)
        except ValueError:
            return None
    return None


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


@router.message(Command("day"))
async def cmd_day(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg:
        target = date.today()
    else:
        target = parse_day(arg)
        if target is None:
            await message.answer(
                "Не понял дату. Форматы: <code>/day 02.07.2024</code>, "
                "<code>/day 2024-07-02</code>, <code>/day 02.07</code> (текущий год), "
                "<code>/day</code> — сегодня."
            )
            return
    await message.answer("🔍 Ищу фото за этот день…")
    try:
        await actions.send_day_photos(message.bot, message.chat.id, target)
    except ServiceError as e:
        await message.answer(f"⚠️ {esc(e.user_message)}")


@router.callback_query(F.data.startswith("day:"))
async def cb_day_more(callback: CallbackQuery) -> None:
    try:
        _, iso_day, offset_str = callback.data.split(":")
        target = date.fromisoformat(iso_day)
        offset = int(offset_str)
    except ValueError:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return
    await callback.answer("Загружаю…")
    if callback.message:
        # Убираем кнопку, чтобы не нажать дважды
        await callback.message.edit_reply_markup(reply_markup=None)
    try:
        await actions.send_day_photos(callback.bot, callback.message.chat.id, target, offset)
    except ServiceError as e:
        if callback.message:
            await callback.message.answer(f"⚠️ {esc(e.user_message)}")


@router.message(Command("memory_today"))
async def cmd_memory_today(message: Message) -> None:
    await message.answer("🕰 Ищу фото, сделанные в этот день в прошлые годы…")
    try:
        await actions.send_memories_today(message.bot, message.chat.id)
    except ServiceError as e:
        await message.answer(f"⚠️ {esc(e.user_message)}")
