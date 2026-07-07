"""SMART-здоровье дисков через smartctl.

Нужен smartmontools в образе и проброс дисков в docker-compose.yml:
devices: [/dev/nvme0n1] + cap_add: [SYS_RAWIO, SYS_ADMIN].
Понимает и NVMe, и SATA (ATA) диски по JSON-выводу smartctl.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field

from app.config import settings
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

# ATA-атрибуты-«состояния»: ненулевое значение — активная проблема прямо сейчас
ATA_STATE_ATTRS = {
    197: "Current_Pending_Sector",
}
# ATA-атрибуты-«счётчики»: копятся за всю жизнь диска. Ненулевое значение может
# быть историей — тревожен только РОСТ (сравнение с базой в monitor).
ATA_COUNTER_ATTRS = {
    5: "Reallocated_Sector_Ct",
    187: "Reported_Uncorrect",
    198: "Offline_Uncorrectable",
}


@dataclass
class SmartInfo:
    device: str
    model: str = "?"
    passed: bool | None = None  # None = smartctl не отдал статус
    temperature: int | None = None
    power_on_hours: int | None = None
    nvme_used_percent: int | None = None  # израсходованный ресурс NVMe
    # Активные состояния — алертить, пока не исчезнут
    state_problems: list[str] = field(default_factory=list)
    # Накопительные счётчики (имя -> raw) — алертить только при росте
    counters: dict[str, int] = field(default_factory=dict)

    @property
    def problems(self) -> list[str]:
        """Всё вместе — для отображения в /smart."""
        return self.state_problems + [f"{k} = {v}" for k, v in self.counters.items()]


def _parse_smart(device: str, data: dict) -> SmartInfo:
    info = SmartInfo(device=device)
    info.model = data.get("model_name") or data.get("model_family") or "?"
    info.passed = data.get("smart_status", {}).get("passed")
    info.temperature = data.get("temperature", {}).get("current")
    info.power_on_hours = data.get("power_on_time", {}).get("hours")

    if info.passed is False:
        info.state_problems.append(
            "SMART-статус FAILED — диск умирает, срочно скопируйте данные!"
        )

    nvme = data.get("nvme_smart_health_information_log")
    if nvme:
        info.nvme_used_percent = nvme.get("percentage_used")
        if nvme.get("critical_warning", 0):
            info.state_problems.append(f"critical_warning = {nvme['critical_warning']}")
        if nvme.get("media_errors", 0):
            info.counters["media_errors"] = nvme["media_errors"]
        if info.nvme_used_percent is not None and info.nvme_used_percent >= 90:
            info.state_problems.append(
                f"ресурс SSD израсходован на {info.nvme_used_percent}%"
            )

    for row in data.get("ata_smart_attributes", {}).get("table", []):
        attr_id = row.get("id")
        raw = row.get("raw", {}).get("value", 0)
        if not isinstance(raw, int) or raw <= 0:
            continue
        if attr_id in ATA_STATE_ATTRS:
            info.state_problems.append(f"{row.get('name', ATA_STATE_ATTRS[attr_id])} = {raw}")
        elif attr_id in ATA_COUNTER_ATTRS:
            info.counters[row.get("name", ATA_COUNTER_ATTRS[attr_id])] = raw

    return info


async def read_smart(device: str) -> SmartInfo:
    try:
        proc = await asyncio.create_subprocess_exec(
            "smartctl", "-H", "-A", "-i", "-j", device,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
    except FileNotFoundError:
        raise ServiceError(
            "smartctl не найден в контейнере — пересоберите образ: "
            "sudo docker compose build homepilot"
        ) from None

    try:
        data = json.loads(out or b"{}")
    except json.JSONDecodeError:
        raise ServiceError(f"smartctl вернул некорректный ответ для {device}.") from None

    # smartctl пишет ошибки доступа в messages, JSON при этом валидный
    errors = [
        m.get("string", "")
        for m in data.get("smartctl", {}).get("messages", [])
        if m.get("severity") == "error"
    ]
    if errors and "smart_status" not in data:
        log.warning("smartctl %s: %s", device, errors[0])
        raise ServiceError(
            f"smartctl не смог открыть {device}: {errors[0]}. "
            "Проверьте devices/cap_add в docker-compose.yml и SMART_DEVICES в .env."
        )
    return _parse_smart(device, data)


async def read_all() -> list[SmartInfo]:
    devices = settings.smart_device_list
    if not devices:
        raise ServiceError(
            "SMART не настроен: задайте SMART_DEVICES в .env "
            "и пробросьте диски в docker-compose.yml (см. README)."
        )
    return [await read_smart(d) for d in devices]
