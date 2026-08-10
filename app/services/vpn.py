"""Управление VPN-сервером (mieru) на VPS по SSH.

Бот живёт в контейнере, сервер — на VPS, поэтому мостик через SSH с отдельным
ключом. На VPS ключ прибит к forced command: что бы бот ни отправил, выполнится
только обёртка `vpn-bot-ctl`, которая принимает лишь действия из ACTIONS и сама
валидирует аргументы. Даже утёкший ключ не даёт shell на VPS.

Обёртка всегда отвечает одной строкой JSON: {"ok": true, ...} либо
{"ok": false, "error": "..."}.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import asyncssh

from app.config import settings
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

ACTIONS = {"status", "users", "add", "del", "link", "sub", "server"}


@dataclass(frozen=True)
class VpnUser:
    name: str
    last_active: datetime | None
    down: int
    up: int
    link: str | None
    sub: str | None

    @property
    def ever_connected(self) -> bool:
        return self.last_active is not None


@dataclass(frozen=True)
class VpnStatus:
    running: bool
    host: str
    port_range: str
    mtu: int | None
    users: int
    tcp_sockets: int
    udp_sockets: int
    connections: int
    sub_port: int
    sub_serving: bool
    total_down: int
    total_up: int
    active_users: int


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def _run(action: str, arg: str | None = None) -> dict:
    """Дёргает обёртку на VPS и возвращает разобранный JSON."""
    if action not in ACTIONS:
        raise ServiceError(f"Действие «{action}» не разрешено.")
    if not settings.vpn_enabled:
        raise ServiceError(
            "Управление VPN не настроено: задайте VPN_SSH_HOST и VPN_SSH_USER в .env "
            "и настройте SSH-ключ (см. README, раздел «Управление VPN»)."
        )
    command = action if arg is None else f"{action} {arg}"
    try:
        async with asyncssh.connect(
            settings.vpn_ssh_host,
            port=settings.vpn_ssh_port,
            username=settings.vpn_ssh_user,
            client_keys=[settings.vpn_ssh_key_path],
            known_hosts=None,
            connect_timeout=15,
        ) as conn:
            result = await conn.run(command, check=False, timeout=90)
    except asyncssh.PermissionDenied as e:
        raise ServiceError(
            "VPS отклонил ключ бота. Проверьте authorized_keys на сервере."
        ) from e
    except (OSError, asyncssh.Error) as e:
        log.warning("SSH до VPS не удался: %s", type(e).__name__)
        raise ServiceError(
            "Не удалось подключиться к VPS. Либо он лежит, либо провайдер "
            "заблокировал его адрес — второе как раз и стоит проверить."
        ) from e

    raw = (result.stdout or "").strip()
    if not raw:
        raise ServiceError("VPS ответил пустотой — проверьте vpn-bot-ctl на сервере.")
    try:
        data = json.loads(raw.splitlines()[-1])
    except json.JSONDecodeError as e:
        log.warning("VPS вернул не-JSON: %s", raw[:200])
        raise ServiceError("VPS ответил неразборчиво — смотрите логи бота.") from e
    if not data.get("ok"):
        raise ServiceError(data.get("error", "неизвестная ошибка на VPS"))
    return data


async def get_status() -> VpnStatus:
    d = await _run("status")
    traffic = d.get("traffic", {}) or {}
    last = d.get("lastActive", {}) or {}
    return VpnStatus(
        running=bool(d.get("running")),
        host=d.get("host", "?"),
        port_range=d.get("portRange", "?"),
        mtu=d.get("mtu"),
        users=int(d.get("users", 0)),
        tcp_sockets=int(d.get("tcpSockets", 0)),
        udp_sockets=int(d.get("udpSockets", 0)),
        connections=int(d.get("connections", 0)),
        sub_port=int(d.get("subPort", 0)),
        sub_serving=bool(d.get("subServing")),
        total_down=sum(v.get("DownloadBytes", 0) for v in traffic.values()),
        total_up=sum(v.get("UploadBytes", 0) for v in traffic.values()),
        active_users=sum(1 for v in last.values() if v),
    )


async def list_users() -> list[VpnUser]:
    d = await _run("users")
    users = [
        VpnUser(
            name=u["name"],
            last_active=_parse_ts(u.get("lastActive")),
            down=int(u.get("down") or 0),
            up=int(u.get("up") or 0),
            link=u.get("link"),
            sub=u.get("sub"),
        )
        for u in d.get("users", [])
    ]
    # Сначала те, кто качает больше — обычно их и ищут
    return sorted(users, key=lambda u: u.down, reverse=True)


async def add_user(name: str) -> tuple[str, str]:
    """Создаёт пользователя, возвращает (прямая ссылка, ссылка на подписку)."""
    d = await _run("add", name)
    return d.get("link") or "", d.get("sub") or ""


async def delete_user(name: str) -> int:
    """Удаляет пользователя, возвращает сколько осталось."""
    d = await _run("del", name)
    return int(d.get("left", 0))


async def get_link(name: str) -> str:
    return (await _run("link", name)).get("value") or ""


async def get_sub(name: str) -> str:
    return (await _run("sub", name)).get("value") or ""


async def set_server(host: str) -> tuple[str, int]:
    """Меняет адрес сервера, возвращает (старый адрес, сколько подписок обновлено)."""
    d = await _run("server", host)
    return d.get("old", "?"), int(d.get("regenerated", 0))


async def probe_port(port: int, timeout: float = 6.0) -> bool:
    """TCP-проба порта VPS из домашней сети.

    Именно так ловится блокировка адреса: сервер жив и из-за границы
    прекрасно доступен, а из России в него уже не достучаться. Ходим
    напрямую, без прокси — иначе проверяли бы сами себя.
    """
    if not settings.vpn_ssh_host or not port:
        return True
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(settings.vpn_ssh_host, port), timeout=timeout
        )
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True
