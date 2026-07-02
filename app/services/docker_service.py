"""Работа с Docker через unix-сокет (примонтирован в контейнер бота).

Все функции синхронные (docker SDK блокирующий) — хендлеры вызывают их
через asyncio.to_thread.
"""

from dataclasses import dataclass

import docker
from docker.errors import DockerException, NotFound

from app.services.errors import ServiceError

_client: docker.DockerClient | None = None

STATUS_EMOJI = {
    "running": "✅",
    "exited": "❌",
    "restarting": "🔄",
    "paused": "⏸",
    "created": "🆕",
    "dead": "💀",
}


@dataclass
class ContainerInfo:
    name: str
    status: str  # running / exited / restarting / paused / created / dead
    image: str
    restart_policy: str

    @property
    def emoji(self) -> str:
        return STATUS_EMOJI.get(self.status, "❔")

    @property
    def is_problem(self) -> bool:
        """Проблемным считаем не-running контейнер, который должен работать постоянно."""
        if self.status == "running":
            return False
        return self.restart_policy in ("always", "unless-stopped") or self.status in (
            "restarting",
            "dead",
        )


def _get_client() -> docker.DockerClient:
    global _client
    if _client is None:
        try:
            _client = docker.from_env()
        except DockerException as e:
            raise ServiceError(
                "Не удалось подключиться к Docker. Проверьте, что "
                "/var/run/docker.sock примонтирован в контейнер бота."
            ) from e
    return _client


def list_containers() -> list[ContainerInfo]:
    try:
        containers = _get_client().containers.list(all=True)
    except DockerException as e:
        raise ServiceError("Docker недоступен: не удалось получить список контейнеров.") from e
    result = []
    for c in containers:
        tags = c.image.tags if c.image else []
        policy = (c.attrs.get("HostConfig", {}).get("RestartPolicy", {}) or {}).get("Name", "")
        result.append(
            ContainerInfo(
                name=c.name,
                status=c.status,
                image=tags[0] if tags else "<none>",
                restart_policy=policy or "no",
            )
        )
    return sorted(result, key=lambda x: (x.status != "running", x.name))


def container_logs(name: str, tail: int = 50) -> str:
    try:
        container = _get_client().containers.get(name)
        return container.logs(tail=tail, timestamps=False).decode("utf-8", errors="replace")
    except NotFound:
        raise ServiceError(f"Контейнер «{name}» не найден.") from None
    except DockerException as e:
        raise ServiceError(f"Не удалось получить логи «{name}».") from e


def restart_container(name: str) -> None:
    try:
        container = _get_client().containers.get(name)
        container.restart(timeout=30)
    except NotFound:
        raise ServiceError(f"Контейнер «{name}» не найден.") from None
    except DockerException as e:
        raise ServiceError(f"Не удалось перезапустить «{name}».") from e
