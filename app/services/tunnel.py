"""Проверка AWG-туннеля до VPS.

Пробуем ICMP-ping внутреннего VPN-адреса VPS (обычно 10.8.1.1). Этот адрес
живёт внутри контейнера amnezia-awg на VPS: TCP-сервисов там нет, но на ping
отвечает само ядро — поэтому ICMP, а не TCP-проба. Хост маршрутизирует
VPN-подсеть в awg0, так что ответ приходит только через живой туннель.
"""

import asyncio
import logging
import re

from app.config import settings

log = logging.getLogger(__name__)

_TIME_RE = re.compile(rb"time=([\d.]+)")
_no_ping_warned = False


async def icmp_ping(host: str, timeout: int = 3) -> float | None:
    """RTT в миллисекундах или None, если хост не ответил."""
    global _no_ping_warned
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", str(timeout), host,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
    except FileNotFoundError:
        if not _no_ping_warned:
            _no_ping_warned = True
            log.warning("Утилита ping не найдена — пересоберите образ бота.")
        return None
    if proc.returncode != 0:
        return None
    return parse_ping_time(out)


def parse_ping_time(output: bytes) -> float:
    match = _TIME_RE.search(output)
    return float(match.group(1)) if match else 0.0


async def check_awg(attempts: int = 1) -> float | None:
    """RTT до VPS через туннель; None = туннель не отвечает или не настроен.

    attempts > 1 — до N пингов с паузой: одиночная потеря пакета
    (обычное дело для UDP-туннеля через DPI-фильтры) не считается падением.
    """
    if not settings.awg_check_host:
        return None
    for i in range(attempts):
        rtt = await icmp_ping(settings.awg_check_host)
        if rtt is not None:
            return rtt
        if i < attempts - 1:
            await asyncio.sleep(1)
    return None
