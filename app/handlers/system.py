"""Команды мониторинга сервера: /status, /disk."""

import asyncio

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.formatting import esc, human_bytes, human_duration, progress_bar
from app.services import system as system_service

router = Router(name="system")


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    st = await system_service.get_status()
    lines = [
        "🖥 <b>Статус сервера</b>\n",
        f"⏱ Аптайм: {human_duration(st.uptime_seconds)}",
        f"🧠 CPU: {st.cpu_percent:.0f}% (load {st.load_avg[0]:.2f} / {st.load_avg[1]:.2f} / {st.load_avg[2]:.2f})",
        f"💾 RAM: {human_bytes(st.ram_used)} / {human_bytes(st.ram_total)} ({st.ram_percent:.0f}%)",
        f"🔄 Swap: {human_bytes(st.swap_used)} / {human_bytes(st.swap_total)} ({st.swap_percent:.0f}%)",
    ]
    if st.battery:
        if st.battery.power_plugged:
            lines.append(f"🔌 Питание: от сети (батарея {st.battery.percent:.0f}%)")
        else:
            left = (
                f", осталось ~{human_duration(st.battery.secsleft)}"
                if st.battery.secsleft
                else ""
            )
            lines.append(
                f"🔋 Питание: ОТ АККУМУЛЯТОРА ({st.battery.percent:.0f}%{left})"
            )
    if st.disks:
        lines.append("\n💽 <b>Диски:</b>")
        for d in st.disks:
            mark = " ⚠️" if d.is_alert else ""
            lines.append(
                f"{esc(d.label)}: {progress_bar(d.percent)} {d.percent:.0f}% "
                f"· свободно {human_bytes(d.free)}{mark}"
            )
    problems = st.problems
    if problems:
        lines.append("")
        for p in problems:
            lines.append(f"⚠️ {esc(p.capitalize())}")
    else:
        lines.append("\n✅ Всё в порядке.")
    await message.answer("\n".join(lines))


@router.message(Command("disk"))
async def cmd_disk(message: Message) -> None:
    disks = await asyncio.to_thread(system_service.get_disks)
    if not disks:
        await message.answer(
            "Не удалось прочитать ни один диск. Проверьте DISK_PATHS в .env "
            "и монтирование /host/root в docker-compose.yml."
        )
        return
    lines = ["💽 <b>Диски</b>\n"]
    for d in disks:
        mark = " ⚠️ почти заполнен!" if d.is_alert else ""
        lines.append(
            f"<b>{esc(d.label)}</b> ({esc(d.path)})\n"
            f"{progress_bar(d.percent)} {d.percent:.0f}%\n"
            f"занято {human_bytes(d.used)} из {human_bytes(d.total)}, "
            f"свободно {human_bytes(d.free)}{mark}\n"
        )
    await message.answer("\n".join(lines))
