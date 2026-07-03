"""Базовые команды: /start, /help, /ping."""

import time

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

router = Router(name="basic")

HELP_TEXT = """🏠 <b>HomePilot</b> — пульт управления домашним сервером.

<b>Сервер</b>
/status — CPU, RAM, swap, аптайм, диски
/disk — свободное место на дисках

<b>Docker</b>
/containers — список контейнеров и статусы
/logs <code>имя [строк]</code> — логи контейнера
/restart <code>имя</code> — перезапуск (только whitelist)

<b>Immich</b>
/immich_status — доступность и статистика
/memory — случайное фото из библиотеки
/memory_today — фото этого дня в прошлые годы
/day <code>[дата]</code> — все фото за день, альбомами по 10

<b>Jellyfin</b>
/jellyfin_status — доступность и новинки
/movie — случайный фильм на вечер

<b>Transmission</b>
/torrents — закачки: статусы, скорость, прогресс
Просто пришлите magnet-ссылку — спрошу, в какую папку
(фильмы/сериалы/музыка), добавлю и сообщу, когда скачается

<b>Zapret (обход DPI)</b>
/zapret — статус, вкл/выкл/перезапуск кнопками

<b>Сводки</b>
/today — что происходит сейчас
/week — итоги недели

/ping — проверка, что бот жив"""


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 Привет! Я <b>HomePilot</b> — бот твоего домашнего сервера.\n\n" + HELP_TEXT
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("ping"))
async def cmd_ping(message: Message) -> None:
    started = time.monotonic()
    reply = await message.answer("🏓 Понг!")
    elapsed_ms = (time.monotonic() - started) * 1000
    await reply.edit_text(f"🏓 Понг! Ответ за {elapsed_ms:.0f} мс.")
