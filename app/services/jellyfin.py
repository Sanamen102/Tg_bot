"""Клиент Jellyfin API (только чтение). Токен передаётся в заголовке."""

import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from app.config import settings
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(20.0)


@dataclass
class MediaItem:
    id: str
    name: str
    item_type: str  # Movie / Episode / Series ...
    year: int | None = None
    overview: str = ""
    genres: list[str] = field(default_factory=list)
    runtime_minutes: int | None = None
    rating: float | None = None
    date_created: datetime | None = None
    series_name: str = ""
    season: int | None = None
    episode: int | None = None

    @property
    def title_line(self) -> str:
        if self.item_type == "Episode" and self.series_name:
            se = ""
            if self.season is not None and self.episode is not None:
                se = f" S{self.season:02d}E{self.episode:02d}"
            return f"📺 {self.series_name}{se} — {self.name}"
        year = f" ({self.year})" if self.year else ""
        return f"🎬 {self.name}{year}"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # Jellyfin отдаёт даты с наносекундами — обрезаем
        return datetime.fromisoformat(value.split(".")[0])
    except ValueError:
        return None


def _item_from_json(data: dict) -> MediaItem:
    ticks = data.get("RunTimeTicks")
    return MediaItem(
        id=data["Id"],
        name=data.get("Name", "Без названия"),
        item_type=data.get("Type", ""),
        year=data.get("ProductionYear"),
        overview=data.get("Overview", "") or "",
        genres=data.get("Genres", []) or [],
        runtime_minutes=ticks // 600_000_000 if ticks else None,
        rating=data.get("CommunityRating"),
        date_created=_parse_dt(data.get("DateCreated")),
        series_name=data.get("SeriesName", "") or "",
        season=data.get("ParentIndexNumber"),
        episode=data.get("IndexNumber"),
    )


FIELDS = "Overview,Genres,ProductionYear,RunTimeTicks,CommunityRating,DateCreated"


class JellyfinClient:
    def __init__(self) -> None:
        if not settings.jellyfin_url or not settings.jellyfin_api_key:
            raise ServiceError(
                "Jellyfin не настроен: задайте JELLYFIN_URL и JELLYFIN_API_KEY в .env."
            )
        self.base_url = settings.jellyfin_url.rstrip("/")
        self._headers = {"X-Emby-Token": settings.jellyfin_api_key}

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, headers=self._headers, timeout=TIMEOUT
            ) as client:
                return await client.get(path, params=params)
        except httpx.HTTPError as e:
            log.warning("Jellyfin недоступен: %s", type(e).__name__)
            raise ServiceError(
                "Jellyfin недоступен: сервер не отвечает. Проверьте, запущен ли контейнер."
            ) from e

    async def ping(self) -> dict:
        resp = await self._get("/System/Info/Public")
        if resp.status_code != 200:
            raise ServiceError(f"Jellyfin вернул ошибку {resp.status_code}.")
        return resp.json()

    async def _items(self, params: dict) -> list[MediaItem]:
        base = {"Recursive": "true", "Fields": FIELDS}
        if settings.jellyfin_user_id:
            base["userId"] = settings.jellyfin_user_id
        base.update(params)
        resp = await self._get("/Items", params=base)
        if resp.status_code in (401, 403):
            raise ServiceError("Jellyfin отклонил API-ключ (401/403). Проверьте JELLYFIN_API_KEY.")
        if resp.status_code != 200:
            raise ServiceError(f"Jellyfin вернул ошибку {resp.status_code} на запрос медиатеки.")
        return [_item_from_json(i) for i in resp.json().get("Items", [])]

    async def latest(self, limit: int = 10) -> list[MediaItem]:
        return await self._items(
            {
                "IncludeItemTypes": "Movie,Episode",
                "SortBy": "DateCreated",
                "SortOrder": "Descending",
                "Limit": str(limit),
            }
        )

    async def random_movie(self) -> MediaItem | None:
        items = await self._items(
            {"IncludeItemTypes": "Movie", "SortBy": "Random", "Limit": "1"}
        )
        return items[0] if items else None

    async def primary_image(self, item_id: str) -> bytes | None:
        resp = await self._get(
            f"/Items/{item_id}/Images/Primary", params={"maxWidth": "800"}
        )
        if resp.status_code != 200:
            return None
        return resp.content
