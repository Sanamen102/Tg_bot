"""Плановые задачи: ежедневное воспоминание, еженедельный отчёт, мониторинг."""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import asyncio

from app import actions, monitor
from app.config import settings
from app.services import metrics
from app.services import system as system_service
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

# Связь с Telegram идёт через туннель и иногда рвётся на секунды. Плановое
# сообщение без повтора просто теряется: 04.10.2026 обрыв пришёлся ровно
# на 18:00, и пропали сразу отчёт недели и воспоминание дня.
RETRY_DELAYS = (60, 300, 900)

# Ошибки socks-прокси aiogram не всегда заворачивает в TelegramNetworkError
# (см. main.py) — повторяем и на них.
try:
    from python_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError

    _NETWORK_ERRORS: tuple[type[BaseException], ...] = (
        TelegramNetworkError, ProxyError, ProxyConnectionError, ProxyTimeoutError,
    )
except ImportError:  # прокси не используется
    _NETWORK_ERRORS = (TelegramNetworkError,)


async def _with_retries(name: str, job) -> None:
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            await job()
            return
        except _NETWORK_ERRORS as e:
            if attempt == len(RETRY_DELAYS):
                log.error("%s: Telegram недоступен, попытки кончились: %s", name, e)
                return
            delay = RETRY_DELAYS[attempt]
            log.warning("%s: нет связи с Telegram (%s), повтор через %d с", name, e, delay)
            await asyncio.sleep(delay)


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
    header_sent = False

    async def job() -> None:
        nonlocal header_sent
        try:
            # Заголовок не повторяем: при обрыве посреди фотографий
            # повтор начнётся с них, а не с ещё одного заголовка
            if not header_sent:
                await bot.send_message(chat_id, "🕰 <b>Воспоминание дня</b>")
                header_sent = True
            await actions.send_memories_today(bot, chat_id, limit=3)
        except ServiceError as e:
            await bot.send_message(chat_id, f"⚠️ Воспоминание дня не получилось: {e.user_message}")

    try:
        await _with_retries("Воспоминание дня", job)
    except Exception:
        log.exception("Ошибка ежедневного воспоминания")


async def _weekly_report(bot: Bot) -> None:
    chat_id = settings.notify_chat_id
    if chat_id is None:
        return

    async def job() -> None:
        text = await actions.build_week_text()
        await bot.send_message(chat_id, text)

    try:
        await _with_retries("Отчёт недели", job)
    except Exception:
        log.exception("Ошибка еженедельного отчёта")


async def _weekly_backup(bot: Bot) -> None:
    chat_id = settings.notify_chat_id
    if chat_id is None:
        return
    from app.handlers.backup import send_backup

    async def job() -> None:
        try:
            await send_backup(bot, chat_id, prefix="🗓 Еженедельный ")
        except ServiceError as e:
            await bot.send_message(chat_id, f"⚠️ Еженедельный бэкап не получился: {e.user_message}")

    try:
        await _with_retries("Бэкап недели", job)
    except Exception:
        log.exception("Ошибка еженедельного бэкапа")


async def _record_metrics() -> None:
    try:
        st = await system_service.get_status()
        await asyncio.to_thread(
            metrics.record, st.cpu_percent, st.ram_percent, st.cpu_temp
        )
    except Exception:
        log.exception("Не удалось записать метрики")


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

    if settings.power_check_interval_seconds > 0:
        scheduler.add_job(
            monitor.power_check,
            "interval",
            seconds=settings.power_check_interval_seconds,
            args=[bot],
            name="power_check",
        )
        log.info("Проверка питания: каждые %d с", settings.power_check_interval_seconds)

    if settings.transmission_url and settings.torrent_check_interval_seconds > 0:
        scheduler.add_job(
            monitor.torrent_check,
            "interval",
            seconds=settings.torrent_check_interval_seconds,
            args=[bot],
            name="torrent_check",
        )
        log.info("Проверка закачек: каждые %d с", settings.torrent_check_interval_seconds)

    if settings.internet_check_interval_seconds > 0:
        scheduler.add_job(
            monitor.internet_check,
            "interval",
            seconds=settings.internet_check_interval_seconds,
            args=[bot],
            name="internet_check",
        )
        log.info(
            "Проверка интернета: каждые %d с", settings.internet_check_interval_seconds
        )

    if settings.metrics_interval_minutes > 0:
        scheduler.add_job(
            _record_metrics,
            "interval",
            minutes=settings.metrics_interval_minutes,
            name="metrics",
        )
        log.info("Запись метрик: каждые %d мин", settings.metrics_interval_minutes)

    backup_at = _parse_hhmm(settings.backup_time)
    if backup_at and settings.backup_paths:
        scheduler.add_job(
            _weekly_backup,
            CronTrigger(
                day_of_week=settings.backup_day, hour=backup_at[0], minute=backup_at[1]
            ),
            args=[bot],
            name="weekly_backup",
        )
        log.info("Бэкап конфигов: %s %02d:%02d", settings.backup_day, *backup_at)

    return scheduler
