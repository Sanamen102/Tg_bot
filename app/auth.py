"""Middleware авторизации: бот отвечает только пользователям из ALLOWED_USER_IDS."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.config import settings

log = logging.getLogger(__name__)


class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None or user.id not in settings.allowed_ids:
            log.warning(
                "Отказано в доступе: user_id=%s username=%s",
                getattr(user, "id", "?"),
                getattr(user, "username", "?"),
            )
            if isinstance(event, Message):
                await event.answer("⛔ Доступ запрещён. Это личный бот.")
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔ Доступ запрещён.", show_alert=True)
            return None
        return await handler(event, data)
