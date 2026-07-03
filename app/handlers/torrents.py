"""Команды Transmission: /torrents и добавление торрента magnet-ссылкой.

Если в .env задан TORRENT_DIRS, после отправки magnet бот спрашивает кнопками,
в какую папку сохранять (фильмы/сериалы/музыка/...). Сама magnet-ссылка
в callback_data не помещается (лимит 64 байта), поэтому она хранится в памяти
по короткому токену до нажатия кнопки.
"""

import secrets
from collections import OrderedDict

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.config import settings
from app.formatting import esc, human_bytes, human_duration, progress_bar
from app.services.errors import ServiceError
from app.services.transmission import TransmissionClient

router = Router(name="torrents")

MAX_NAME = 60
MAX_SHOWN = 20

# токен -> magnet-ссылка, ждущая выбора папки
_pending: OrderedDict[str, str] = OrderedDict()
_PENDING_MAX = 20


def _short(name: str) -> str:
    return name if len(name) <= MAX_NAME else name[: MAX_NAME - 1] + "…"


@router.message(Command("torrents"))
async def cmd_torrents(message: Message) -> None:
    try:
        torrents = await TransmissionClient().torrents()
    except ServiceError as e:
        await message.answer(f"⚠️ {esc(e.user_message)}")
        return
    if not torrents:
        await message.answer(
            "Закачек нет. Пришлите magnet-ссылку — добавлю в Transmission."
        )
        return

    downloading = sum(1 for t in torrents if t.status == 4)
    seeding = sum(1 for t in torrents if t.status == 6)
    paused = sum(1 for t in torrents if t.status == 0)
    lines = [
        f"⬇️ <b>Transmission</b> — всего {len(torrents)}: "
        f"качается {downloading}, раздаётся {seeding}, на паузе {paused}\n"
    ]
    for t in torrents[:MAX_SHOWN]:
        lines.append(f"{t.status_label} <b>{esc(_short(t.name))}</b>")
        if t.status == 4:  # качается — прогресс, скорость, ETA
            details = [
                f"{progress_bar(t.percent * 100)} {t.percent * 100:.0f}%",
                f"{human_bytes(t.rate_down)}/с",
            ]
            if t.eta > 0:
                details.append(f"осталось ~{human_duration(t.eta)}")
            lines.append("  " + " · ".join(details))
        else:
            lines.append(f"  {human_bytes(t.size)} · {t.percent * 100:.0f}%")
    if len(torrents) > MAX_SHOWN:
        lines.append(f"\n…и ещё {len(torrents) - MAX_SHOWN}.")
    await message.answer("\n".join(lines))


async def _do_add(message: Message, magnet: str, download_dir: str | None, dir_label: str) -> None:
    try:
        name, duplicate = await TransmissionClient().add_magnet(magnet, download_dir)
    except ServiceError as e:
        await message.answer(f"⚠️ {esc(e.user_message)}")
        return
    if duplicate:
        await message.answer(f"ℹ️ Такой торрент уже есть: <b>{esc(name)}</b>")
    else:
        await message.answer(
            f"✅ Добавил в закачки: <b>{esc(name)}</b>\n"
            f"📁 Куда: {esc(dir_label)}\n"
            "Пришлю сообщение, когда скачается. Статус: /torrents"
        )


@router.message(F.text.startswith("magnet:?"))
async def add_magnet(message: Message) -> None:
    magnet = message.text.strip()
    categories = settings.torrent_categories
    if not categories:
        # Папки не настроены — сразу в дефолтную папку Transmission
        await _do_add(message, magnet, None, "папка Transmission по умолчанию")
        return

    token = secrets.token_urlsafe(6)
    _pending[token] = magnet
    while len(_pending) > _PENDING_MAX:
        _pending.popitem(last=False)

    rows = []
    for i in range(0, len(categories), 2):
        rows.append(
            [
                InlineKeyboardButton(text=label, callback_data=f"tdl:{token}:{idx}")
                for idx, label in (
                    (j, categories[j][0]) for j in range(i, min(i + 2, len(categories)))
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="📥 По умолчанию", callback_data=f"tdl:{token}:-1"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"tdl:{token}:x"),
        ]
    )
    await message.answer(
        "📁 Куда сохранить закачку?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("tdl:"))
async def cb_add_magnet(callback: CallbackQuery) -> None:
    try:
        _, token, idx = callback.data.split(":")
    except ValueError:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    magnet = _pending.get(token)
    if magnet is None:
        await callback.answer(
            "Эта ссылка уже устарела — пришлите magnet ещё раз.", show_alert=True
        )
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
        return

    if idx == "x":
        _pending.pop(token, None)
        if callback.message:
            await callback.message.edit_text("Добавление отменено.")
        await callback.answer()
        return

    download_dir = None
    dir_label = "папка Transmission по умолчанию"
    if idx != "-1":
        categories = settings.torrent_categories
        try:
            dir_label, download_dir = categories[int(idx)]
        except (ValueError, IndexError):
            await callback.answer("Категория не найдена — проверьте TORRENT_DIRS.", show_alert=True)
            return

    _pending.pop(token, None)
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(f"⏳ Добавляю в «{esc(dir_label)}»…")
        await _do_add(callback.message, magnet, download_dir, dir_label)
