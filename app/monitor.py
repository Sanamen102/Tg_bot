"""Фоновый мониторинг: алерты о заполненных дисках и упавших контейнерах.

Каждая проблема алертится один раз; когда она исчезает — приходит сообщение
о восстановлении, и алерт может сработать снова.
"""

import asyncio
import logging

from aiogram import Bot

from app.config import settings
from app.formatting import esc, human_bytes
from app.services import docker_service
from app.services import system as system_service
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

# ключ проблемы -> текст алерта
_active_alerts: dict[str, str] = {}


async def _collect_problems() -> dict[str, str]:
    problems: dict[str, str] = {}

    try:
        disks = await asyncio.to_thread(system_service.get_disks)
        for d in disks:
            if d.is_alert:
                problems[f"disk:{d.label}"] = (
                    f"💽 Диск «{esc(d.label)}» заполнен на {d.percent:.0f}% "
                    f"(свободно {human_bytes(d.free)})."
                )
    except Exception:
        log.exception("Мониторинг: не удалось проверить диски")

    try:
        containers = await asyncio.to_thread(docker_service.list_containers)
        for c in containers:
            if c.is_problem:
                problems[f"container:{c.name}"] = (
                    f"🐳 Контейнер «{esc(c.name)}» в состоянии «{esc(c.status)}», "
                    f"хотя должен работать. Логи: /logs {esc(c.name)}"
                )
    except ServiceError as e:
        log.warning("Мониторинг: %s", e.user_message)
    except Exception:
        log.exception("Мониторинг: не удалось проверить контейнеры")

    return problems


async def monitor_check(bot: Bot) -> None:
    chat_id = settings.notify_chat_id
    if chat_id is None:
        return

    problems = await _collect_problems()

    new_keys = set(problems) - set(_active_alerts)
    resolved_keys = set(_active_alerts) - set(problems)

    if new_keys:
        lines = ["🚨 <b>HomePilot: обнаружены проблемы</b>\n"]
        lines += [problems[k] for k in sorted(new_keys)]
        await bot.send_message(chat_id, "\n".join(lines))

    if resolved_keys:
        lines = ["✅ <b>HomePilot: проблемы устранены</b>\n"]
        lines += [f"• {k.split(':', 1)[1]} снова в порядке" for k in sorted(resolved_keys)]
        await bot.send_message(chat_id, "\n".join(lines))

    _active_alerts.clear()
    _active_alerts.update(problems)
