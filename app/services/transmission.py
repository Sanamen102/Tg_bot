"""Клиент Transmission RPC.

Протокол: POST на /transmission/rpc с JSON. Transmission требует CSRF-токен
X-Transmission-Session-Id: первый запрос получает 409 с токеном в заголовке,
после чего запрос повторяется. Токен кешируется на классе.
"""

import logging
from dataclasses import dataclass

import httpx

from app.config import settings
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(20.0)

# https://github.com/transmission/transmission/blob/main/docs/rpc-spec.md
STATUS_LABEL = {
    0: "⏸ пауза",
    1: "🔍 ждёт проверки",
    2: "🔍 проверяется",
    3: "⌛ в очереди",
    4: "⬇️ качается",
    5: "⌛ ждёт раздачи",
    6: "⬆️ раздаётся",
}

FIELDS = [
    "id",
    "name",
    "status",
    "percentDone",
    "rateDownload",
    "rateUpload",
    "eta",
    "totalSize",
    "isFinished",
]


@dataclass
class Torrent:
    id: int
    name: str
    status: int
    percent: float  # 0.0 .. 1.0
    rate_down: int  # байт/с
    rate_up: int
    eta: int  # секунды; отрицательное = неизвестно
    size: int
    is_finished: bool

    @property
    def status_label(self) -> str:
        return STATUS_LABEL.get(self.status, f"❔ статус {self.status}")

    @property
    def is_done(self) -> bool:
        return self.is_finished or self.percent >= 1.0


class TransmissionClient:
    # Токен сессии живёт на классе — переживает создание новых клиентов
    _session_id: str = ""

    def __init__(self) -> None:
        if not settings.transmission_url:
            raise ServiceError("Transmission не настроен: задайте TRANSMISSION_URL в .env.")
        self.rpc_url = settings.transmission_url.rstrip("/") + "/transmission/rpc"
        self.auth = (
            (settings.transmission_user, settings.transmission_password)
            if settings.transmission_user
            else None
        )

    async def _rpc(self, method: str, arguments: dict | None = None) -> dict:
        payload: dict = {"method": method}
        if arguments:
            payload["arguments"] = arguments
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, auth=self.auth) as client:
                resp = await client.post(
                    self.rpc_url,
                    json=payload,
                    headers={"X-Transmission-Session-Id": type(self)._session_id},
                )
                if resp.status_code == 409:
                    type(self)._session_id = resp.headers.get("X-Transmission-Session-Id", "")
                    resp = await client.post(
                        self.rpc_url,
                        json=payload,
                        headers={"X-Transmission-Session-Id": type(self)._session_id},
                    )
        except httpx.HTTPError as e:
            log.warning("Transmission недоступен: %s", type(e).__name__)
            raise ServiceError(
                "Transmission недоступен: сервер не отвечает. Проверьте, запущен ли контейнер."
            ) from e
        if resp.status_code == 401:
            raise ServiceError(
                "Transmission отклонил логин/пароль (401). "
                "Проверьте TRANSMISSION_USER и TRANSMISSION_PASSWORD."
            )
        if resp.status_code != 200:
            raise ServiceError(f"Transmission вернул ошибку {resp.status_code}.")
        data = resp.json()
        if data.get("result") != "success":
            raise ServiceError(f"Transmission: {data.get('result', 'неизвестная ошибка')}.")
        return data.get("arguments", {})

    async def torrents(self) -> list[Torrent]:
        args = await self._rpc("torrent-get", {"fields": FIELDS})
        result = []
        for t in args.get("torrents", []):
            result.append(
                Torrent(
                    id=t["id"],
                    name=t.get("name", "?"),
                    status=t.get("status", 0),
                    percent=t.get("percentDone", 0.0),
                    rate_down=t.get("rateDownload", 0),
                    rate_up=t.get("rateUpload", 0),
                    eta=t.get("eta", -1),
                    size=t.get("totalSize", 0),
                    is_finished=t.get("isFinished", False),
                )
            )
        # Качающиеся сверху, потом по имени
        return sorted(result, key=lambda t: (t.is_done, t.name.lower()))

    async def add_magnet(self, magnet: str) -> tuple[str, bool]:
        """Добавляет magnet-ссылку. Возвращает (имя, уже_был_добавлен)."""
        args = await self._rpc("torrent-add", {"filename": magnet})
        if "torrent-duplicate" in args:
            return args["torrent-duplicate"].get("name", "торрент"), True
        return args.get("torrent-added", {}).get("name", "торрент"), False
