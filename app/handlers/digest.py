"""Сводки: /today и /week."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app import actions

router = Router(name="digest")


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    waiting = await message.answer("⏳ Собираю сводку…")
    text = await actions.build_today_text()
    await waiting.edit_text(text)


@router.message(Command("week"))
async def cmd_week(message: Message) -> None:
    waiting = await message.answer("⏳ Собираю сводку за неделю…")
    text = await actions.build_week_text()
    await waiting.edit_text(text)
