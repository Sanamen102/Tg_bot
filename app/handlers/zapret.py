"""Команда /zapret: статус и управление обходом DPI на хосте."""

from datetime import datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.formatting import esc
from app.services import zapret
from app.services.errors import ServiceError

router = Router(name="zapret")

MAX_STATUS_CHARS = 1500


def _keyboard(active: bool) -> InlineKeyboardMarkup:
    if active:
        toggle = InlineKeyboardButton(text="⏹ Выключить", callback_data="zapret:stop")
    else:
        toggle = InlineKeyboardButton(text="▶️ Включить", callback_data="zapret:start")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [toggle, InlineKeyboardButton(text="🔄 Перезапустить", callback_data="zapret:restart")],
            [InlineKeyboardButton(text="♻️ Обновить статус", callback_data="zapret:refresh")],
        ]
    )


async def _status_message() -> tuple[str, bool]:
    active = await zapret.is_active()
    header = "🛡 <b>Zapret</b>: " + ("✅ активен" if active else "❌ выключен")
    details = await zapret.run_action("status")
    if details:
        details = esc(details[-MAX_STATUS_CHARS:])
        header += f"\n<pre>{details}</pre>"
    header += f"\n<i>обновлено {datetime.now():%H:%M:%S}</i>"
    return header, active


@router.message(Command("zapret"))
async def cmd_zapret(message: Message) -> None:
    try:
        text, active = await _status_message()
    except ServiceError as e:
        await message.answer(f"⚠️ {esc(e.user_message)}")
        return
    await message.answer(text, reply_markup=_keyboard(active))


@router.callback_query(F.data.startswith("zapret:"))
async def cb_zapret(callback: CallbackQuery) -> None:
    action = callback.data.split(":", 1)[1]
    if action not in ("start", "stop", "restart", "refresh"):
        await callback.answer("Неизвестное действие.", show_alert=True)
        return
    await callback.answer("Выполняю…")
    try:
        if action != "refresh":
            await zapret.run_action(action)
        text, active = await _status_message()
    except ServiceError as e:
        if callback.message:
            await callback.message.answer(f"⚠️ {esc(e.user_message)}")
        return
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=_keyboard(active))
        except TelegramBadRequest:
            # "message is not modified" — статус не изменился, это не ошибка
            pass
