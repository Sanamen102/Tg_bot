"""Таймлапс-камера: /cam — что сейчас за окном, /camstatus — как идёт съёмка."""

import asyncio
from datetime import datetime

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, FSInputFile, Message

from app.formatting import esc, human_bytes, human_duration
from app.services import camera
from app.services.errors import ServiceError

router = Router(name="camera")

NOT_CONFIGURED = "📷 Камера не настроена: задайте CAMERA_URL и TIMELAPSE_DIR в .env."


def _phone_line(info: camera.PhoneInfo) -> str:
    parts = []
    if info.battery is not None:
        parts.append(f"🔋 {info.battery:.0f}%")
    if info.temp is not None:
        parts.append(f"🌡 {info.temp:.0f} °C")
    if info.plugged is False:
        parts.append("⚠️ питание отключено")
    elif info.charging:
        parts.append("заряжается")
    return " · ".join(parts)


def _frame_time(path) -> str:
    # имя кадра — ГГГГММДД_ЧЧММ.jpg
    try:
        return datetime.strptime(path.stem, "%Y%m%d_%H%M").strftime("%d.%m %H:%M")
    except ValueError:
        return path.name


@router.message(Command("cam"))
async def cmd_cam(message: Message) -> None:
    if not camera.configured():
        await message.answer(NOT_CONFIGURED)
        return
    await message.answer("📷 Снимаю…")
    shot, info = await asyncio.gather(camera.snapshot(), camera.phone_info(), return_exceptions=True)

    if isinstance(shot, bytes):
        caption = f"📷 Сейчас за окном · {datetime.now():%H:%M}"
        if isinstance(info, camera.PhoneInfo) and (line := _phone_line(info)):
            caption += f"\n{line}"
        await message.answer_photo(BufferedInputFile(shot, filename="cam.jpg"), caption=caption)
        return

    # Телефон не ответил — покажем хотя бы последний кадр таймлапса
    reason = shot.user_message if isinstance(shot, ServiceError) else "неизвестная ошибка"
    last = camera.latest_frame()
    if last is None:
        await message.answer(f"⚠️ {esc(reason)}")
        return
    await message.answer_photo(
        FSInputFile(last),
        caption=f"⚠️ {esc(reason)}\nПоследний кадр таймлапса: {_frame_time(last)}",
    )


@router.message(Command("camstatus"))
async def cmd_camstatus(message: Message) -> None:
    if not camera.configured():
        await message.answer(NOT_CONFIGURED)
        return
    st = camera.state()
    now = datetime.now()
    lines = ["📷 <b>Таймлапс</b>"]

    last_ok = st.get("last_ok")
    if last_ok:
        ts = datetime.fromisoformat(last_ok).replace(tzinfo=None)
        ago = human_duration((now - ts).total_seconds())
        lines.append(f"Последний кадр: {ts:%d.%m %H:%M} ({ago} назад)")
    else:
        lines.append("Кадров ещё не было")

    day = camera.shooting_day(now)
    if day.total:
        lines.append(f"Сегодня: {day.ok} из {day.total} кадров, {human_bytes(day.bytes)}")
    else:
        lines.append("Сегодня ещё не снимали (ночью съёмки нет)")

    try:
        info = await camera.phone_info()
        line = _phone_line(info)
        lines.append("Телефон: на связи" + (f" · {line}" if line else ""))
    except ServiceError as e:
        lines.append(f"Телефон: ⚠️ {esc(e.user_message)}")

    if st.get("last_error"):
        lines.append(f"Последняя ошибка съёмки: <code>{esc(st['last_error'])}</code>")
    await message.answer("\n".join(lines))
