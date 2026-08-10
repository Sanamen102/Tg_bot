"""Базовые команды: /start, /help, /ping."""

import time

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

router = Router(name="basic")

HELP_TEXT = """🏠 <b>HomePilot</b> — пульт управления домашним сервером.

<b>Сервер</b>
/status — CPU, RAM, swap, аптайм, диски, питание, туннель
/smart — SMART-здоровье дисков
/graph <code>[часов]</code> — график CPU/RAM/°C (по умолчанию 24 ч)
/backup — архив конфигов сервера прямо в чат

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

<b>Скачать видео или картинки по ссылке</b>
Пришлите ссылку на YouTube, TikTok, Instagram, VK,
Rutube, Pinterest и т.п. — предложу прислать видео в чат
(сохранить на телефон), положить в медиатеку или вытащить
звук. Посты с картинками и слайдшоу пришлю альбомом

<b>Книги</b>
Пришлите файл .epub/.pdf/.fb2 — положу в библиотеку Kavita,
читать можно с любого устройства с сохранением места

<b>Zapret (обход DPI)</b>
/zapret — статус, вкл/выкл/перезапуск кнопками

<b>VPN (mieru на VPS)</b>
/vpn — статус сервера: порты, соединения, трафик
/vpn_users — кто заходил и сколько накачал
/vpn_add <code>имя</code> — выдать доступ: пришлю ссылку и QR
/vpn_link <code>имя</code> — показать ссылку и QR ещё раз
/vpn_del <code>имя</code> — отозвать доступ у одного
/vpn_server <code>адрес</code> — переезд на новый сервер:
перепишет все подписки разом, людей обходить не нужно

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
