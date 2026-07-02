"""Плановые задачи: ежедневное воспоминание, еженедельный отчёт, мониторинг."""

import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app import actions, monitor
from app.config import settings
from app.services.errors import ServiceError

log = logging.getLogger(__name__)


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    try:
        hour, minute = value.strip().split(":")
        return int(hour), int(minute)
    except (ValueError, AttributeError):
        return None


async def _daily_memory(bot: Bot) -> None:
    chat_id = settings.notify_chat_id
    if chat_id is None:
        return
    try:
        await bot.send_message(chat_id, "🕰 <b>Воспоминание дня</b>")
        await actions.send_memories_today(bot, chat_id, limit=3)
    except ServiceError as e:
        await bot.send_message(chat_id, f"⚠️ Воспоминание дня не получилось: {e.user_message}")
    except Exception:
        log.exception("Ошибка ежедневного воспоминания")


async def _weekly_report(bot: Bot) -> None:
    chat_id = settings.notify_chat_id
    if chat_id is None:
        return
    try:
        text = await actions.build_week_text()
        await bot.send_message(chat_id, text)
    except Exception:
        log.exception("Ошибка еженедельного отчёта")


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()

    daily = _parse_hhmm(settings.daily_memory_time)
    if daily:
        scheduler.add_job(
            _daily_memory,
            CronTrigger(hour=daily[0], minute=daily[1]),
            args=[bot],
            name="daily_memory",
        )
        log.info("Ежедневное воспоминание: %02d:%02d", *daily)

    weekly = _parse_hhmm(settings.weekly_report_time)
    if weekly:
        scheduler.add_job(
            _weekly_report,
            CronTrigger(day_of_week=settings.weekly_report_day, hour=weekly[0], minute=weekly[1]),
            args=[bot],
            name="weekly_report",
        )
        log.info("Еженедельный отчёт: %s %02d:%02d", settings.weekly_report_day, *weekly)

    if settings.monitor_interval_minutes > 0:
        scheduler.add_job(
            monitor.monitor_check,
            "interval",
            minutes=settings.monitor_interval_minutes,
            args=[bot],
            name="monitor",
        )
        log.info("Мониторинг: каждые %d мин", settings.monitor_interval_minutes)

    return scheduler
