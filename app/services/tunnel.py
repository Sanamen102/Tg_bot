"""Проверка AWG-туннеля до VPS.

Бот живёт в docker-сети, но хост маршрутизирует VPN-подсеть (10.8.1.0/24)
в awg0, поэтому TCP-проба внутреннего адреса VPS из контейнера проходит
только через живой туннель. Если туннель лежит, пакет уходит в дефолтный
маршрут и приватный адрес недостижим — получаем таймаут.
"""

import asyncio
import time

from app.config import settings


async def tcp_probe(host: str, port: int, timeout: float = 3.0) -> float | None:
    """RTT в миллисекундах или None, если соединиться не удалось."""
    start = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout
        )
    except (OSError, asyncio.TimeoutError):
        return None
    rtt_ms = (time.monotonic() - start) * 1000
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return rtt_ms


async def check_awg() -> float | None:
    """RTT до VPS через туннель; None = туннель не отвечает или не настроен."""
    if not settings.awg_check_host:
        return None
    return await tcp_probe(settings.awg_check_host, settings.awg_check_port)
