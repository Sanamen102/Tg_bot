"""Команды Transmission: /torrents и добавление торрента magnet-ссылкой."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.formatting import esc, human_bytes, human_duration, progress_bar
from app.services.errors import ServiceError
from app.services.transmission import TransmissionClient

router = Router(name="torrents")

MAX_NAME = 60
MAX_SHOWN = 20


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


@router.message(F.text.startswith("magnet:?"))
async def add_magnet(message: Message) -> None:
    try:
        name, duplicate = await TransmissionClient().add_magnet(message.text.strip())
    except ServiceError as e:
        await message.answer(f"⚠️ {esc(e.user_message)}")
        return
    if duplicate:
        await message.answer(f"ℹ️ Такой торрент уже есть: <b>{esc(name)}</b>")
    else:
        await message.answer(
            f"✅ Добавил в закачки: <b>{esc(name)}</b>\n"
            "Пришлю сообщение, когда скачается. Статус: /torrents"
        )
