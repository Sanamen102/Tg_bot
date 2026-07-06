"""Команды мониторинга сервера: /status, /disk."""

import asyncio

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.config import settings
from app.formatting import esc, human_bytes, human_duration, progress_bar
from app.services import smart as smart_service
from app.services import system as system_service
from app.services import tunnel as tunnel_service
from app.services.errors import ServiceError

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
    if st.cpu_temp is not None:
        temp_mark = " 🔥" if st.cpu_temp >= settings.temp_alert_threshold else ""
        lines.append(f"🌡 Температура CPU: {st.cpu_temp:.0f}°C{temp_mark}")
    if st.battery:
        wear = (
            f", износ {st.battery.wear_percent:.0f}%"
            if st.battery.wear_percent is not None
            else ""
        )
        if st.battery.charge_limit and st.battery.charge_limit < 100:
            wear += f", лимит заряда {st.battery.charge_limit}%"
        if st.battery.power_plugged:
            lines.append(f"🔌 Питание: от сети (батарея {st.battery.percent:.0f}%{wear})")
        else:
            left = (
                f", осталось ~{human_duration(st.battery.secsleft)}"
                if st.battery.secsleft
                else ""
            )
            lines.append(
                f"🔋 Питание: ОТ АККУМУЛЯТОРА ({st.battery.percent:.0f}%{left})"
            )
    if settings.awg_check_host:
        rtt = await tunnel_service.check_awg()
        if rtt is not None:
            lines.append(f"🔒 Туннель AWG до VPS: ✅ {rtt:.0f} мс")
        else:
            lines.append("🔒 Туннель AWG до VPS: ❌ не отвечает")
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


@router.message(Command("smart"))
async def cmd_smart(message: Message) -> None:
    try:
        infos = await smart_service.read_all()
    except ServiceError as e:
        await message.answer(f"⚠️ {esc(e.user_message)}")
        return
    lines = ["💽 <b>SMART-здоровье дисков</b>\n"]
    for info in infos:
        if info.passed is True:
            status = "✅ PASSED"
        elif info.passed is False:
            status = "❌ FAILED"
        else:
            status = "❔ статус неизвестен"
        lines.append(f"<b>{esc(info.device)}</b> — {esc(info.model)}")
        details = [status]
        if info.temperature is not None:
            details.append(f"🌡 {info.temperature}°C")
        if info.power_on_hours:
            years = info.power_on_hours / 24 / 365.25
            details.append(f"наработка {info.power_on_hours} ч (~{years:.1f} г.)")
        if info.nvme_used_percent is not None:
            details.append(f"ресурс SSD: изношен на {info.nvme_used_percent}%")
        lines.append("  " + " · ".join(details))
        for p in info.problems:
            lines.append(f"  ⚠️ {esc(p)}")
        lines.append("")
    await message.answer("\n".join(lines).rstrip())


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
