"""Метрики сервера: CPU, RAM, swap, uptime, диски.

Бот работает в контейнере, но /proc внутри контейнера показывает метрики хоста
(CPU, память, uptime). Диски хоста читаются через ро-монтирование корня в /host/root.
"""

import asyncio
import os
import time
from dataclasses import dataclass, field

import psutil

from app.config import settings


@dataclass
class DiskInfo:
    label: str
    path: str
    total: int
    used: int
    free: int
    percent: float

    @property
    def is_alert(self) -> bool:
        return self.percent >= settings.disk_alert_threshold


@dataclass
class SystemStatus:
    uptime_seconds: float
    cpu_percent: float
    load_avg: tuple[float, float, float]
    ram_total: int
    ram_used: int
    ram_percent: float
    swap_total: int
    swap_used: int
    swap_percent: float
    disks: list[DiskInfo] = field(default_factory=list)

    @property
    def problems(self) -> list[str]:
        issues = []
        for d in self.disks:
            if d.is_alert:
                issues.append(f"диск «{d.label}» заполнен на {d.percent:.0f}%")
        if self.ram_percent >= 95:
            issues.append(f"RAM почти исчерпана ({self.ram_percent:.0f}%)")
        cpu_count = os.cpu_count() or 1
        if self.load_avg[1] > cpu_count * 2:
            issues.append(f"высокая нагрузка (load {self.load_avg[1]:.2f})")
        return issues


def get_disks() -> list[DiskInfo]:
    disks = []
    for label, path in settings.disks:
        try:
            usage = psutil.disk_usage(path)
        except OSError:
            continue
        disks.append(
            DiskInfo(
                label=label,
                path=path,
                total=usage.total,
                used=usage.used,
                free=usage.free,
                percent=usage.percent,
            )
        )
    return disks


def _collect() -> SystemStatus:
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    try:
        load = psutil.getloadavg()
    except OSError:
        load = (0.0, 0.0, 0.0)
    return SystemStatus(
        uptime_seconds=time.time() - psutil.boot_time(),
        cpu_percent=psutil.cpu_percent(interval=0.5),
        load_avg=load,
        ram_total=mem.total,
        ram_used=mem.used,
        ram_percent=mem.percent,
        swap_total=swap.total,
        swap_used=swap.used,
        swap_percent=swap.percent,
        disks=get_disks(),
    )


async def get_status() -> SystemStatus:
    # cpu_percent(interval=0.5) блокирует — уносим в поток
    return await asyncio.to_thread(_collect)
