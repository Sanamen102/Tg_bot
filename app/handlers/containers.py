"""Команды Docker: /containers, /logs, /restart (с подтверждением через inline-кнопки)."""

import asyncio

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.config import settings
from app.formatting import esc
from app.services import docker_service
from app.services.errors import ServiceError

router = Router(name="containers")

MAX_LOG_CHARS = 3500
MAX_LOG_LINES = 200


@router.message(Command("containers"))
async def cmd_containers(message: Message) -> None:
    try:
        containers = await asyncio.to_thread(docker_service.list_containers)
    except ServiceError as e:
        await message.answer(f"⚠️ {esc(e.user_message)}")
        return
    if not containers:
        await message.answer("Контейнеров нет.")
        return
    running = sum(1 for c in containers if c.status == "running")
    lines = [f"🐳 <b>Контейнеры</b> ({running}/{len(containers)} запущено)\n"]
    for c in containers:
        lines.append(f"{c.emoji} <b>{esc(c.name)}</b> — {esc(c.status)} · <i>{esc(c.image)}</i>")
    problems = [c for c in containers if c.is_problem]
    if problems:
        lines.append("\n⚠️ <b>Что сломалось:</b>")
        for c in problems:
            lines.append(
                f"• {esc(c.name)} в состоянии «{esc(c.status)}», хотя должен работать "
                f"(restart={esc(c.restart_policy)}). Логи: /logs {esc(c.name)}"
            )
    await message.answer("\n".join(lines))


@router.message(Command("logs"))
async def cmd_logs(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    if not args:
        try:
            containers = await asyncio.to_thread(docker_service.list_containers)
            names = ", ".join(esc(c.name) for c in containers) or "—"
        except ServiceError:
            names = "—"
        await message.answer(
            "Использование: <code>/logs имя_контейнера [строк]</code>\n"
            f"Доступные контейнеры: {names}"
        )
        return
    name = args[0]
    tail = 50
    if len(args) > 1:
        try:
            tail = max(1, min(MAX_LOG_LINES, int(args[1])))
        except ValueError:
            pass
    try:
        logs = await asyncio.to_thread(docker_service.container_logs, name, tail)
    except ServiceError as e:
        await message.answer(f"⚠️ {esc(e.user_message)}")
        return
    if not logs.strip():
        await message.answer(f"Логи «{esc(name)}» пусты.")
        return
    text = esc(logs)
    if len(text) > MAX_LOG_CHARS:
        text = "…\n" + text[-MAX_LOG_CHARS:]
    await message.answer(
        f"📜 Логи <b>{esc(name)}</b> (последние {tail} строк):\n<pre>{text}</pre>"
    )


@router.message(Command("restart"))
async def cmd_restart(message: Message, command: CommandObject) -> None:
    whitelist = settings.restart_whitelist
    name = (command.args or "").strip()
    if not name:
        allowed = ", ".join(esc(n) for n in whitelist) if whitelist else "— (whitelist пуст)"
        await message.answer(
            "Использование: <code>/restart имя_контейнера</code>\n"
            f"Разрешены к перезапуску: {allowed}"
        )
        return
    if name not in whitelist:
        await message.answer(
            f"🚫 Контейнер «{esc(name)}» не входит в whitelist перезапуска.\n"
            "Добавьте его в DOCKER_RESTART_WHITELIST в .env, если это осознанное решение."
        )
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Перезапустить", callback_data=f"restart:{name}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="restart_cancel"),
            ]
        ]
    )
    await message.answer(
        f"Перезапустить контейнер <b>{esc(name)}</b>?", reply_markup=keyboard
    )


@router.callback_query(F.data == "restart_cancel")
async def cb_restart_cancel(callback: CallbackQuery) -> None:
    if callback.message:
        await callback.message.edit_text("Перезапуск отменён.")
    await callback.answer()


@router.callback_query(F.data.startswith("restart:"))
async def cb_restart(callback: CallbackQuery) -> None:
    name = callback.data.split(":", 1)[1]
    # Повторная проверка whitelist — на случай, если конфиг сменился
    if name not in settings.restart_whitelist:
        await callback.answer("Контейнер не в whitelist.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text(f"🔄 Перезапускаю <b>{esc(name)}</b>…")
    try:
        await asyncio.to_thread(docker_service.restart_container, name)
    except ServiceError as e:
        if callback.message:
            await callback.message.edit_text(f"⚠️ {esc(e.user_message)}")
        await callback.answer()
        return
    if callback.message:
        await callback.message.edit_text(f"✅ Контейнер <b>{esc(name)}</b> перезапущен.")
    await callback.answer("Готово")
