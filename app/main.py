"""Точка входа HomePilot."""

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import BotCommand

log = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand(command="status", description="Статус сервера"),
    BotCommand(command="disk", description="Свободное место на дисках"),
    BotCommand(command="containers", description="Docker-контейнеры"),
    BotCommand(command="logs", description="Логи контейнера"),
    BotCommand(command="restart", description="Перезапустить контейнер"),
    BotCommand(command="immich_status", description="Статус Immich"),
    BotCommand(command="memory", description="Случайное фото"),
    BotCommand(command="memory_today", description="Фото этого дня в прошлом"),
    BotCommand(command="jellyfin_status", description="Статус Jellyfin"),
    BotCommand(command="movie", description="Случайный фильм на вечер"),
    BotCommand(command="today", description="Сводка на сегодня"),
    BotCommand(command="week", description="Сводка за неделю"),
    BotCommand(command="ping", description="Проверка связи"),
    BotCommand(command="help", description="Справка по командам"),
]


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Конфиг валидируем до импорта остальных модулей, чтобы ошибка .env
    # выводилась понятным сообщением, а не трейсбеком из недр импортов.
    try:
        from app.config import settings
    except Exception as e:
        log.error("Ошибка конфигурации: %s", e)
        log.error("Проверьте .env — обязательны BOT_TOKEN и ALLOWED_USER_IDS.")
        sys.exit(1)

    if not settings.allowed_ids:
        log.error("ALLOWED_USER_IDS пуст — бот никому не будет отвечать. Задайте свой user_id.")
        sys.exit(1)

    from app.auth import AuthMiddleware
    from app.handlers import basic, containers, digest, immich, jellyfin, system
    from app.scheduler import setup_scheduler

    session = None
    if settings.telegram_proxy:
        from aiogram.client.session.aiohttp import AiohttpSession

        session = AiohttpSession(proxy=settings.telegram_proxy)
        log.info("Telegram API через прокси (адрес скрыт из логов).")

    bot = Bot(
        settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # Авторизация на все входящие сообщения и нажатия кнопок
    dp.message.outer_middleware(AuthMiddleware())
    dp.callback_query.outer_middleware(AuthMiddleware())

    dp.include_routers(
        basic.router,
        system.router,
        containers.router,
        immich.router,
        jellyfin.router,
        digest.router,
    )

    # Меню команд — не критично: если Telegram сейчас недоступен,
    # не падаем, а идём в polling (он сам переподключается с backoff).
    try:
        await bot.set_my_commands(BOT_COMMANDS)
    except TelegramNetworkError as e:
        log.warning(
            "Не удалось задать меню команд (нет связи с Telegram): %s. "
            "Продолжаю запуск — polling будет пытаться переподключиться.",
            e,
        )

    scheduler = setup_scheduler(bot)
    scheduler.start()

    log.info("HomePilot запущен. Разрешённые пользователи: %d шт.", len(settings.allowed_ids))
    try:
        # start_polling первым делом дергает bot.me() — если Telegram сейчас
        # недоступен (сеть, блокировка), не падаем, а повторяем попытки.
        while True:
            try:
                await dp.start_polling(bot)
                break  # штатная остановка polling
            except TelegramNetworkError as e:
                log.warning(
                    "Нет связи с Telegram API: %s. Повторная попытка через 30 с. "
                    "Если провайдер блокирует Telegram — задайте TELEGRAM_PROXY в .env.",
                    e,
                )
                await asyncio.sleep(30)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
