"""Команда /graph — график CPU/RAM/температуры за последние N часов."""

import asyncio
import time
from datetime import datetime

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from app.config import settings
from app.services import metrics

router = Router(name="graph")

MAX_HOURS = 14 * 24


def _render(rows: list[tuple], hours: int) -> bytes:
    # matplotlib импортируем лениво: тяжёлый, нужен только этой команде
    import io

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    ts = [datetime.fromtimestamp(r[0]) for r in rows]
    cpu = [r[1] for r in rows]
    ram = [r[2] for r in rows]
    temp = [r[3] if r[3] is not None else float("nan") for r in rows]
    has_temp = any(r[3] is not None for r in rows)

    fig, ax = plt.subplots(figsize=(10, 5), dpi=110)
    ax.plot(ts, cpu, label="CPU %", color="#1f77b4", linewidth=1.4)
    ax.plot(ts, ram, label="RAM %", color="#2ca02c", linewidth=1.4)
    ax.set_ylim(0, 100)
    ax.set_ylabel("%")
    ax.grid(alpha=0.3)

    handles, labels = ax.get_legend_handles_labels()
    if has_temp:
        ax2 = ax.twinx()
        ax2.plot(ts, temp, label="CPU °C", color="#d62728", linewidth=1.2, alpha=0.85)
        ax2.set_ylabel("°C")
        top = max(t for t in temp if t == t)  # nan-безопасный максимум
        ax2.set_ylim(20, max(90.0, top + 10))
        h2, l2 = ax2.get_legend_handles_labels()
        handles += h2
        labels += l2

    ax.legend(handles, labels, loc="upper left", ncol=3, framealpha=0.9)
    if hours <= 48:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    else:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m %H:%M"))
    fig.autofmt_xdate()
    ax.set_title(f"Сервер за последние {hours} ч")
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


@router.message(Command("graph"))
async def cmd_graph(message: Message, command: CommandObject) -> None:
    if settings.metrics_interval_minutes <= 0:
        await message.answer(
            "Сбор метрик выключен (METRICS_INTERVAL_MINUTES=0) — графику не из чего строиться."
        )
        return
    hours = 24
    arg = (command.args or "").strip()
    if arg:
        try:
            hours = max(1, min(MAX_HOURS, int(arg)))
        except ValueError:
            await message.answer(
                "Использование: <code>/graph [часов]</code>, например <code>/graph 48</code>."
            )
            return

    since = int(time.time()) - hours * 3600
    rows = await asyncio.to_thread(metrics.fetch, since)
    if len(rows) < 2:
        await message.answer(
            f"Пока мало данных: собираю метрики раз в {settings.metrics_interval_minutes} мин, "
            "загляните чуть позже."
        )
        return

    png = await asyncio.to_thread(_render, rows, hours)

    cpu_values = [r[1] for r in rows]
    ram_values = [r[2] for r in rows]
    temps = [r[3] for r in rows if r[3] is not None]
    caption = (
        f"📈 За {hours} ч · CPU ср {sum(cpu_values) / len(cpu_values):.0f}% "
        f"(макс {max(cpu_values):.0f}%) · RAM ср {sum(ram_values) / len(ram_values):.0f}%"
    )
    if temps:
        caption += f" · 🌡 макс {max(temps):.0f}°C"
    await message.answer_photo(
        BufferedInputFile(png, filename="server-graph.png"), caption=caption
    )
