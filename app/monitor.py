"""Фоновый мониторинг: алерты о заполненных дисках и упавших контейнерах.

Каждая проблема алертится один раз; когда она исчезает — приходит сообщение
о восстановлении, и алерт может сработать снова.
"""

import asyncio
import logging
from datetime import datetime

import httpx
from aiogram import Bot

from app.config import settings
from app.formatting import esc, human_bytes, human_duration
from app.services import docker_service
from app.services import metrics
from app.services import smart as smart_service
from app.services import system as system_service
from app.services import tunnel as tunnel_service
from app.services import zapret as zapret_service
from app.services.errors import ServiceError
from app.services.transmission import TransmissionClient

log = logging.getLogger(__name__)

# ключ проблемы -> текст алерта
_active_alerts: dict[str, str] = {}

# Состояние питания для детектора отключения света (None = ещё не знаем)
_last_plugged: bool | None = None
_low_battery_alerted = False
_charge_limit_warned = False


async def _ensure_charge_limit() -> None:
    """Поддерживает лимит заряда: sysfs сбрасывается на 100 после ребута хоста."""
    global _charge_limit_warned
    if not settings.battery_charge_limit:
        return
    try:
        supported = await asyncio.to_thread(
            system_service.apply_charge_limit, settings.battery_charge_limit
        )
        if not supported and not _charge_limit_warned:
            _charge_limit_warned = True
            log.warning(
                "BATTERY_CHARGE_LIMIT задан, но ноутбук не поддерживает "
                "charge_control_end_threshold — лимит не применён."
            )
    except PermissionError as e:
        if not _charge_limit_warned:
            _charge_limit_warned = True
            log.warning(
                "%s. Проверьте, что /sys/class/power_supply смонтирован rw "
                "в docker-compose.yml.",
                e,
            )


async def power_check(bot: Bot) -> None:
    """Частая проверка питания: алерт при переходе на аккумулятор и обратно."""
    global _last_plugged, _low_battery_alerted

    chat_id = settings.notify_chat_id
    if chat_id is None:
        return
    await _ensure_charge_limit()
    battery = await asyncio.to_thread(system_service.get_battery)
    if battery is None:
        return

    if _last_plugged is None:
        # Первый запуск: запоминаем состояние без алерта
        _last_plugged = battery.power_plugged
        return

    if battery.power_plugged != _last_plugged:
        # Состояние фиксируем ТОЛЬКО после успешной отправки: если Telegram
        # сейчас недоступен (при отключении света роутер тоже гаснет),
        # исключение оставит старое состояние и алерт уйдёт со следующей попытки.
        if battery.power_plugged:
            await bot.send_message(
                chat_id,
                f"🔌 <b>Свет дали!</b> Сервер снова питается от сети "
                f"(батарея {battery.percent:.0f}%).",
            )
            _low_battery_alerted = False
        else:
            left = (
                f", по оценке хватит на ~{human_duration(battery.secsleft)}"
                if battery.secsleft
                else ""
            )
            await bot.send_message(
                chat_id,
                f"⚡ <b>Похоже, выключили свет!</b> Сервер перешёл на аккумулятор: "
                f"заряд {battery.percent:.0f}%{left}.",
            )
        _last_plugged = battery.power_plugged

    if (
        not battery.power_plugged
        and battery.percent <= settings.battery_low_threshold
        and not _low_battery_alerted
    ):
        _low_battery_alerted = True
        await bot.send_message(
            chat_id,
            f"🪫 <b>Критично: заряд {battery.percent:.0f}%!</b> "
            "Света всё нет, сервер скоро выключится.",
        )


# Сколько циклов подряд AWG-туннель не отвечает (защита от морганий)
_awg_fail_cycles = 0


async def _collect_problems() -> tuple[dict[str, str], list[str]]:
    """Возвращает (постоянные проблемы, разовые сообщения).

    Постоянные живут в _active_alerts (алерт + «снова в порядке»),
    разовые (например, рост SMART-счётчика) отправляются один раз.
    """
    global _awg_fail_cycles
    problems: dict[str, str] = {}
    oneoffs: list[str] = []

    try:
        disks = await asyncio.to_thread(system_service.get_disks)
        for d in disks:
            if d.is_alert:
                problems[f"disk:{d.label}"] = (
                    f"💽 Диск «{esc(d.label)}» заполнен на {d.percent:.0f}% "
                    f"(свободно {human_bytes(d.free)})."
                )
    except Exception:
        log.exception("Мониторинг: не удалось проверить диски")

    try:
        containers = await asyncio.to_thread(docker_service.list_containers)
        for c in containers:
            if c.is_problem:
                problems[f"container:{c.name}"] = (
                    f"🐳 Контейнер «{esc(c.name)}» в состоянии «{esc(c.status)}», "
                    f"хотя должен работать. Логи: /logs {esc(c.name)}"
                )
    except ServiceError as e:
        log.warning("Мониторинг: %s", e.user_message)
    except Exception:
        log.exception("Мониторинг: не удалось проверить контейнеры")

    if settings.zapret_enabled:
        try:
            if not await zapret_service.is_active():
                problems["zapret"] = (
                    "🛡 Zapret не активен — обход DPI не работает. Включить: /zapret"
                )
        except ServiceError as e:
            log.warning("Мониторинг zapret: %s", e.user_message)
        except Exception:
            log.exception("Мониторинг: не удалось проверить zapret")

    try:
        temp = await asyncio.to_thread(system_service.get_cpu_temp)
        if temp and temp >= settings.temp_alert_threshold:
            problems["temp:CPU"] = (
                f"🌡 CPU перегрет: {temp:.0f}°C (порог {settings.temp_alert_threshold}°C). "
                "Проверьте вентиляцию ноутбука."
            )
    except Exception:
        log.exception("Мониторинг: не удалось проверить температуру")

    if settings.smart_device_list:
        try:
            for info in await smart_service.read_all():
                # Состояния (FAILED, pending-сектора...) — обычный алерт,
                # висит, пока проблема не исчезнет
                if info.state_problems:
                    problems[f"smart:{info.device}"] = (
                        f"💽 SMART {esc(info.device)} ({esc(info.model)}): "
                        + "; ".join(esc(p) for p in info.state_problems)
                    )
                # Накопительные счётчики: сравниваем с базой в SQLite,
                # шумим только при первом обнаружении и при росте
                for attr, value in sorted(info.counters.items()):
                    baseline = await asyncio.to_thread(
                        metrics.get_smart_baseline, info.device, attr
                    )
                    if baseline is None:
                        await asyncio.to_thread(
                            metrics.set_smart_baseline, info.device, attr, value
                        )
                        oneoffs.append(
                            f"💽 SMART {esc(info.device)}: {esc(attr)} = {value}. "
                            "Зафиксировал как базовый уровень — теперь предупрежу "
                            "только если счётчик начнёт расти."
                        )
                    elif value > baseline:
                        await asyncio.to_thread(
                            metrics.set_smart_baseline, info.device, attr, value
                        )
                        oneoffs.append(
                            f"🚨 💽 SMART {esc(info.device)}: {esc(attr)} ВЫРОС "
                            f"{baseline} → {value} — диск деградирует! "
                            "Проверьте /smart и планируйте замену."
                        )
        except ServiceError as e:
            log.warning("Мониторинг SMART: %s", e.user_message)
        except Exception:
            log.exception("Мониторинг: не удалось проверить SMART")

    if settings.awg_check_host:
        try:
            # До 3 пингов за проверку + подтверждение несколькими циклами:
            # короткие моргания UDP-туннеля не должны будить хозяина
            if await tunnel_service.check_awg(attempts=3) is None:
                _awg_fail_cycles += 1
                if _awg_fail_cycles == 1:
                    log.warning("AWG-туннель не ответил (цикл 1) — жду подтверждения")
            else:
                _awg_fail_cycles = 0
            if _awg_fail_cycles >= settings.awg_confirm_fails:
                minutes = _awg_fail_cycles * max(settings.monitor_interval_minutes, 1)
                problems["awg:туннель до VPS"] = (
                    f"🔒 AWG-туннель до VPS не отвечает уже ~{minutes} мин — "
                    "доступ к дому извне не работает. "
                    "Проверьте: systemctl status awg-quick@awg0 и awg show."
                )
        except Exception:
            log.exception("Мониторинг: не удалось проверить AWG-туннель")

    for label, url in settings.watch_services:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.get(url)
            if resp.status_code >= 500:
                problems[f"web:{label}"] = (
                    f"🌐 Сервис «{esc(label)}» отвечает ошибкой {resp.status_code}."
                )
        except httpx.HTTPError:
            problems[f"web:{label}"] = f"🌐 Сервис «{esc(label)}» недоступен."

    return problems, oneoffs


async def monitor_check(bot: Bot) -> None:
    chat_id = settings.notify_chat_id
    if chat_id is None:
        return

    problems, oneoffs = await _collect_problems()

    if oneoffs:
        await bot.send_message(chat_id, "\n\n".join(oneoffs))

    new_keys = set(problems) - set(_active_alerts)
    resolved_keys = set(_active_alerts) - set(problems)

    if new_keys:
        lines = ["🚨 <b>HomePilot: обнаружены проблемы</b>\n"]
        lines += [problems[k] for k in sorted(new_keys)]
        await bot.send_message(chat_id, "\n".join(lines))

    if resolved_keys:
        lines = ["✅ <b>HomePilot: проблемы устранены</b>\n"]
        lines += [f"• {k.split(':', 1)[-1]} снова в порядке" for k in sorted(resolved_keys)]
        await bot.send_message(chat_id, "\n".join(lines))

    _active_alerts.clear()
    _active_alerts.update(problems)


# ---------- Завершённые закачки Transmission ----------

# id торрента -> был ли завершён при прошлой проверке (None = ещё не смотрели)
_torrent_done: dict[int, bool] | None = None


async def torrent_check(bot: Bot) -> None:
    """Алерт, когда качавшийся торрент завершился."""
    global _torrent_done

    chat_id = settings.notify_chat_id
    if chat_id is None or not settings.transmission_url:
        return
    try:
        torrents = await TransmissionClient().torrents()
    except ServiceError as e:
        log.warning("Проверка торрентов: %s", e.user_message)
        return

    current = {t.id: t.is_done for t in torrents}
    if _torrent_done is None:
        # Первый запуск: запоминаем состояние, о старых закачках не алертим
        _torrent_done = current
        return

    for t in torrents:
        if t.is_done and _torrent_done.get(t.id) is False:
            await bot.send_message(
                chat_id,
                f"✅ <b>Скачалось:</b> {esc(t.name)} ({human_bytes(t.size)})",
            )
    _torrent_done = current


# ---------- Детектор падений интернета ----------

# Проверяем прямое TCP-соединение до надёжных адресов (без DNS и без прокси)
_NET_CHECK_HOSTS = (("1.1.1.1", 443), ("8.8.8.8", 443))
_NET_CONFIRM_FAILS = 2  # сколько проверок подряд должно упасть, чтобы считать сбоем

_net_outage_start: datetime | None = None
_net_fail_count = 0


async def _internet_ok() -> bool:
    for host, port in _NET_CHECK_HOSTS:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=5
            )
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return True
        except (OSError, asyncio.TimeoutError):
            continue
    return False


async def internet_check(bot: Bot) -> None:
    """Пост-фактум отчёт: «интернет пропадал с X до Y» после восстановления связи."""
    global _net_outage_start, _net_fail_count

    chat_id = settings.notify_chat_id
    if chat_id is None:
        return

    if await _internet_ok():
        if _net_outage_start and _net_fail_count >= _NET_CONFIRM_FAILS:
            start = _net_outage_start
            end = datetime.now()
            duration = human_duration((end - start).total_seconds())
            # Сначала шлём отчёт, потом сбрасываем состояние: если Telegram ещё
            # не доступен, исключение сохранит сбой до следующей попытки
            await bot.send_message(
                chat_id,
                f"🌐 <b>Интернет вернулся!</b> Связь пропадала с "
                f"{start:%H:%M} до {end:%H:%M} (~{duration}).",
            )
        _net_outage_start = None
        _net_fail_count = 0
    else:
        _net_fail_count += 1
        if _net_outage_start is None:
            _net_outage_start = datetime.now()
        if _net_fail_count == _NET_CONFIRM_FAILS:
            log.warning("Интернет недоступен с %s", _net_outage_start)
