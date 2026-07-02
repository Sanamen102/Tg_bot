"""Клиент Immich API (только чтение). Ключ передаётся в заголовке и не попадает в логи."""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx

from app.config import settings
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(20.0)


@dataclass
class Asset:
    id: str
    type: str  # IMAGE / VIDEO
    taken_at: datetime | None

    @property
    def is_video(self) -> bool:
        return self.type == "VIDEO"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _asset_from_json(item: dict) -> Asset:
    taken = item.get("localDateTime") or item.get("fileCreatedAt")
    return Asset(id=item["id"], type=item.get("type", "IMAGE"), taken_at=_parse_dt(taken))


class ImmichClient:
    def __init__(self) -> None:
        if not settings.immich_url or not settings.immich_api_key:
            raise ServiceError("Immich не настроен: задайте IMMICH_URL и IMMICH_API_KEY в .env.")
        self.base_url = settings.immich_url.rstrip("/")
        self._headers = {
            "x-api-key": settings.immich_api_key,
            "Accept": "application/json",
        }

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, headers=self._headers, timeout=TIMEOUT
            ) as client:
                return await client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            log.warning("Immich недоступен: %s", type(e).__name__)
            raise ServiceError(
                "Immich недоступен: сервер не отвечает. Проверьте, запущен ли контейнер."
            ) from e

    async def ping(self) -> bool:
        for path in ("/api/server/ping", "/api/server-info/ping"):
            resp = await self._request("GET", path)
            if resp.status_code == 200:
                return True
        return False

    async def version(self) -> str:
        resp = await self._request("GET", "/api/server/version")
        if resp.status_code == 200:
            data = resp.json()
            return f'{data.get("major", "?")}.{data.get("minor", "?")}.{data.get("patch", "?")}'
        return "?"

    async def statistics(self) -> dict | None:
        """Общая статистика (нужен админский API-ключ). None, если прав не хватает."""
        resp = await self._request("GET", "/api/server/statistics")
        if resp.status_code in (401, 403):
            return None
        if resp.status_code != 200:
            raise ServiceError(f"Immich вернул ошибку {resp.status_code} на запрос статистики.")
        return resp.json()

    async def random_assets(self, count: int = 1) -> list[Asset]:
        resp = await self._request("POST", "/api/search/random", json={"size": count})
        if resp.status_code != 200:
            raise ServiceError(f"Immich вернул ошибку {resp.status_code} на случайный поиск.")
        data = resp.json()
        items = data if isinstance(data, list) else data.get("assets", {}).get("items", [])
        return [_asset_from_json(i) for i in items]

    async def search_metadata(self, payload: dict) -> list[Asset]:
        resp = await self._request("POST", "/api/search/metadata", json=payload)
        if resp.status_code != 200:
            raise ServiceError(f"Immich вернул ошибку {resp.status_code} на поиск по метаданным.")
        items = resp.json().get("assets", {}).get("items", [])
        return [_asset_from_json(i) for i in items]

    async def assets_taken_on(self, day: date, size: int = 5) -> list[Asset]:
        start = datetime(day.year, day.month, day.day)
        end = start + timedelta(days=1)
        return await self.search_metadata(
            {
                "takenAfter": start.isoformat(),
                "takenBefore": end.isoformat(),
                "size": size,
            }
        )

    async def memories_today(
        self, years_back: int = 15, per_year: int = 3
    ) -> list[tuple[int, list[Asset]]]:
        """Фото/видео, снятые в этот же день в прошлые годы. [(годов_назад, ассеты), ...]"""
        today = date.today()
        memories = []
        for delta in range(1, years_back + 1):
            try:
                past_day = today.replace(year=today.year - delta)
            except ValueError:  # 29 февраля
                continue
            try:
                assets = await self.assets_taken_on(past_day, size=per_year)
            except ServiceError:
                raise
            if assets:
                memories.append((delta, assets))
        return memories

    async def count_created_since(self, since: datetime, cap: int = 1000) -> int:
        """Сколько ассетов загружено с указанной даты (не более cap)."""
        assets = await self.search_metadata(
            {"createdAfter": since.isoformat(), "size": cap}
        )
        return len(assets)

    async def thumbnail(self, asset_id: str) -> bytes:
        resp = await self._request(
            "GET", f"/api/assets/{asset_id}/thumbnail", params={"size": "preview"}
        )
        if resp.status_code != 200:
            raise ServiceError("Immich не отдал превью для этого файла.")
        return resp.content
