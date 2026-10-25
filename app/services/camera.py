"""Таймлапс-камера: свежий кадр с телефона и состояние съёмки.

Камера — старый телефон с Android IP Camera у окна; кадры раз в 10 минут
забирает скрипт таймлапса на хосте. Бот ничего не настраивает и в съёмку не
вмешивается: только читает. Логин камеры берётся из camera.env скрипта —
в .env бота его дублировать не нужно.

Осторожно с паролем: после 5 отказов подряд приложение блокирует адрес на
15 минут, а адрес у бота и у скрипта один (хост). Поэтому на 401/403 сразу
отдаём ошибку и не повторяем запрос.
"""

import csv
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from app.config import settings
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(40.0, connect=5.0)


@dataclass
class PhoneInfo:
    battery: float | None
    temp: float | None
    charging: bool | None
    plugged: bool | None


@dataclass
class ShootingDay:
    ok: int
    total: int
    bytes: int


def configured() -> bool:
    return bool(settings.camera_url and settings.timelapse_dir)


def _credentials() -> tuple[str, str]:
    path = Path(settings.timelapse_dir) / "camera.env"
    env = {}
    try:
        for line in path.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
        return env["CAM_USER"], env["CAM_PASS"]
    except (OSError, KeyError) as e:
        raise ServiceError("Не прочитать логин камеры (camera.env скрипта таймлапса).") from e


async def _get(path: str) -> httpx.Response:
    # Сертификат на телефоне самоподписанный, сеть домашняя
    async with httpx.AsyncClient(verify=False, timeout=TIMEOUT, auth=_credentials()) as client:
        try:
            resp = await client.get(settings.camera_url.rstrip("/") + path)
        except httpx.HTTPError as e:
            log.warning("camera %s: %s", path, type(e).__name__)
            raise ServiceError("Телефон-камера не отвечает — выключен или не в сети.") from e
    if resp.status_code in (401, 403):
        raise ServiceError("Камера отклонила логин и пароль — проверьте camera.env.")
    if resp.status_code == 503:
        raise ServiceError("Камера на телефоне не готова (служба стартует или выключена).")
    if resp.status_code != 200:
        raise ServiceError(f"Камера ответила {resp.status_code}.")
    return resp


async def snapshot() -> bytes:
    """Полноразмерный кадр прямо сейчас (~2 с, 2–3 МБ)."""
    jpg = (await _get("/video/snapshot")).content
    if jpg[:2] != b"\xff\xd8":
        raise ServiceError("Камера прислала не JPEG.")
    return jpg


async def phone_info() -> PhoneInfo:
    try:
        d = (await _get("/info.json")).json()
    except ValueError as e:
        raise ServiceError("Камера прислала непонятный ответ.") from e
    batt = d.get("batteryPercent")
    return PhoneInfo(
        battery=float(batt) if isinstance(batt, (int, float)) and batt >= 0 else None,
        temp=d.get("batteryTempC"),       # есть только в нашей сборке приложения
        charging=d.get("batteryCharging"),
        plugged=d.get("batteryPlugged"),
    )


def latest_frame() -> Path | None:
    """Последний сохранённый кадр таймлапса (раскладка ГГГГ/ММ/ДД/*.jpg)."""
    root = Path(settings.timelapse_frames_dir) if settings.timelapse_frames_dir else None
    if not root or not root.is_dir():
        return None
    node = root
    for _ in range(3):  # год → месяц → день: берём самую позднюю папку
        dirs = sorted(p for p in node.iterdir() if p.is_dir())
        if not dirs:
            return None
        node = dirs[-1]
    frames = sorted(node.glob("*.jpg"))
    return frames[-1] if frames else None


def state() -> dict:
    try:
        return json.loads((Path(settings.timelapse_dir) / "state.json").read_text())
    except (OSError, ValueError):
        return {}


def shooting_day(day: datetime) -> ShootingDay:
    """Сколько кадров снято за день по журналу скрипта (log/ГГГГ-ММ.csv)."""
    path = Path(settings.timelapse_dir) / "log" / f"{day:%Y-%m}.csv"
    prefix = day.strftime("%Y-%m-%d")
    ok = total = size = 0
    try:
        with path.open() as f:
            for row in csv.DictReader(f):
                if not row["time"].startswith(prefix):
                    continue
                total += 1
                if row["ok"] == "1":
                    ok += 1
                    size += int(row["bytes"] or 0)
    except (OSError, KeyError, ValueError):
        pass
    return ShootingDay(ok, total, size)
