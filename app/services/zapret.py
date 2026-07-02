"""Управление systemd-сервисом zapret на хосте по SSH.

Бот в контейнере, zapret — на хосте, поэтому мостик через SSH с отдельным
ключом. На хосте ключ ограничен forced command (см. README): что бы бот ни
отправил, выполнится только скрипт-обёртка, который принимает лишь действия
из ACTIONS. Даже утёкший ключ не даёт shell на хосте.
"""

import logging

import asyncssh

from app.config import settings
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

ACTIONS = {"start", "stop", "restart", "status", "is-active"}


async def run_action(action: str) -> str:
    if action not in ACTIONS:
        raise ServiceError(f"Действие «{action}» не разрешено.")
    if not settings.zapret_enabled:
        raise ServiceError(
            "Управление zapret не настроено: задайте ZAPRET_SSH_USER в .env "
            "и настройте SSH-ключ (см. README, раздел «Управление zapret»)."
        )
    try:
        async with asyncssh.connect(
            settings.zapret_ssh_host,
            port=settings.zapret_ssh_port,
            username=settings.zapret_ssh_user,
            client_keys=[settings.zapret_ssh_key_path],
            # Хост в локальной докерной сети; ключей хоста заранее нет
            known_hosts=None,
            connect_timeout=10,
        ) as conn:
            result = await conn.run(action, check=False, timeout=30)
            output = ((result.stdout or "") + (result.stderr or "")).strip()
            if result.exit_status != 0 and action != "is-active":
                # is-active возвращает не-0 для inactive — это не ошибка
                log.warning("zapret %s завершился с кодом %s", action, result.exit_status)
            return output
    except asyncssh.PermissionDenied as e:
        raise ServiceError(
            "SSH отклонил ключ бота. Проверьте authorized_keys на хосте."
        ) from e
    except (OSError, asyncssh.Error) as e:
        log.warning("SSH до хоста не удался: %s", type(e).__name__)
        raise ServiceError(
            "Не удалось подключиться к хосту по SSH. Проверьте, что sshd запущен "
            "и ZAPRET_SSH_HOST/PORT в .env верные."
        ) from e


async def is_active() -> bool:
    state = await run_action("is-active")
    return state.strip() == "active"
