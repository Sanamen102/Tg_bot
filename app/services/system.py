"""Метрики сервера: CPU, RAM, swap, uptime, диски.

Бот работает в контейнере, но /proc внутри контейнера показывает метрики хоста
(CPU, память, uptime). Диски хоста читаются через ро-монтирование корня в /host/root.
"""

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from app.config import settings

# Датчики температуры CPU в порядке предпочтения (Intel, AMD, ARM, ACPI)
_TEMP_SENSORS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz")


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
class BatteryInfo:
    percent: float
    power_plugged: bool
    secsleft: int | None  # None = неизвестно или заряжается
    wear_percent: float | None = None  # износ: насколько ёмкость меньше заводской


def _read_battery_wear() -> float | None:
    """Износ батареи из sysfs: 100% * (1 - текущая_ёмкость / заводская)."""
    base = Path("/sys/class/power_supply")
    try:
        for bat in sorted(base.glob("BAT*")):
            for full_name, design_name in (
                ("energy_full", "energy_full_design"),
                ("charge_full", "charge_full_design"),
            ):
                full_f = bat / full_name
                design_f = bat / design_name
                if full_f.exists() and design_f.exists():
                    full = int(full_f.read_text().strip())
                    design = int(design_f.read_text().strip())
                    if full > 0 and design > 0:
                        return max(0.0, 100.0 * (1 - full / design))
    except (OSError, ValueError):
        pass
    return None


def get_cpu_temp() -> float | None:
    """Максимальная температура CPU в °C. None — датчики недоступны."""
    try:
        temps = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return None
    if not temps:
        return None

    def valid(entries) -> list[float]:
        return [e.current for e in entries if e.current and 0 < e.current < 130]

    for name in _TEMP_SENSORS:
        values = valid(temps.get(name, []))
        if values:
            return max(values)
    values = valid([e for entries in temps.values() for e in entries])
    return max(values) if values else None


def get_battery() -> BatteryInfo | None:
    """Состояние питания. None — батареи нет (или её не видно из контейнера)."""
    try:
        batt = psutil.sensors_battery()
    except Exception:
        return None
    if batt is None:
        return None
    secs = batt.secsleft
    if secs in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN) or secs < 0:
        secs = None
    return BatteryInfo(
        percent=batt.percent,
        power_plugged=bool(batt.power_plugged),
        secsleft=secs,
        wear_percent=_read_battery_wear(),
    )


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
    battery: BatteryInfo | None = None
    cpu_temp: float | None = None

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
        if self.battery and not self.battery.power_plugged:
            issues.append(
                f"работает от аккумулятора ({self.battery.percent:.0f}%) — возможно, нет света"
            )
        if self.cpu_temp and self.cpu_temp >= settings.temp_alert_threshold:
            issues.append(f"CPU перегрет ({self.cpu_temp:.0f}°C)")
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
        battery=get_battery(),
        cpu_temp=get_cpu_temp(),
    )


async def get_status() -> SystemStatus:
    # cpu_percent(interval=0.5) блокирует — уносим в поток
    return await asyncio.to_thread(_collect)
