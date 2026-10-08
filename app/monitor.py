"""Фоновый мониторинг: алерты о заполненных дисках и упавших контейнерах.

Каждая проблема алертится один раз; когда она исчезает — приходит сообщение
о восстановлении, и алерт может сработать снова.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TypeVar

import httpx
import psutil
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from app.config import settings
from app.formatting import esc, human_bytes, human_duration
from app.services import docker_service
from app.services import metrics
from app.services import photo_backup
from app.services import smart as smart_service
from app.services import system as system_service
from app.services import tunnel as tunnel_service
from app.services import vpn as vpn_service
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

# То же для VPN-сервера: сколько циклов подряд его порт молчит из дома
_vpn_fail_cycles = 0

# Когда порт VPN замолчал. Пока VPS лежит, бот сам без Telegram — он ходит
# туда через этот же VPS, — и тревога не уходит, а после восстановления
# проблема просто исчезала из списка. Так 01–02.10.2026 незамеченным прошёл
# 11-часовой простой Аезы. Теперь отчитываемся задним числом.
_vpn_outage_start: datetime | None = None

# Сообщения, которые надо доставить, даже если первая попытка не прошла:
# сразу после восстановления VPN мост бота может ещё не переподключиться.
# Сюда же идут разовые сообщения (рост SMART-счётчика): базовый уровень в
# SQLite к моменту отправки уже обновлён, и при обрыве связи алерт «диск
# деградирует» иначе не пришёл бы никогда.
_pending_reports: list[str] = []
# В очереди лежит отчёт о простое VPN — он заменяет обычное «снова в порядке»
_vpn_report_queued = False

# Проверки, не вернувшиеся с прошлого цикла. Повисший вызов (statfs на
# подвисшем mergerfs, docker API, smartctl на умирающем диске) раньше держал
# monitor_check целиком, и APScheduler молча пропускал все следующие запуски:
# алерты пропадали до перезапуска бота. Теперь зависшая проверка сама
# становится алертом, а остальные идут своим чередом. Новую копию не
# запускаем, пока старая не вернулась: иначе потоки повисших вызовов копились
# бы в пуле и рано или поздно заняли бы его целиком.
_hung: dict[str, asyncio.Future] = {}

T = TypeVar("T")


class _Hung(Exception):
    def __init__(self, name: str, timeout: float) -> None:
        super().__init__(name)
        self.name = name
        self.timeout = timeout


async def _guarded(name: str, factory: Callable[[], Awaitable[T]], timeout: float) -> T:
    prev = _hung.get(name)
    if prev is not None:
        if not prev.done():
            raise _Hung(name, timeout)
        _hung.pop(name)
        if not prev.cancelled():
            prev.exception()  # забираем, чтобы asyncio не ругался «never retrieved»
    task = asyncio.ensure_future(factory())
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout)
    except TimeoutError:
        _hung[name] = task
        raise _Hung(name, timeout) from None


def _hung_problem(problems: dict[str, str], e: _Hung) -> None:
    log.warning("Мониторинг: проверка «%s» не вернулась за %.0f с", e.name, e.timeout)
    problems[f"hung:{e.name}"] = (
        f"⏳ Проверка «{esc(e.name)}» не отвечает дольше {e.timeout:.0f} с — "
        "что-то зависло (диск, docker или сеть). Остальные проверки работают."
    )


async def _collect_problems() -> tuple[dict[str, str], list[str]]:
    """Возвращает (постоянные проблемы, разовые сообщения).

    Постоянные живут в _active_alerts (алерт + «снова в порядке»),
    разовые (например, рост SMART-счётчика) отправляются один раз.
    """
    global _awg_fail_cycles, _vpn_fail_cycles, _vpn_outage_start, _vpn_report_queued
    problems: dict[str, str] = {}
    oneoffs: list[str] = []

    try:
        disks = await _guarded(
            "место на дисках", lambda: asyncio.to_thread(system_service.get_disks), 60
        )
        seen = {d.label for d in disks}
        for d in disks:
            if not d.mounted:
                problems[f"unmounted:{d.label}"] = (
                    f"💽 Диск «{esc(d.label)}» не смонтирован: на месте {esc(d.path)} "
                    "пустая папка системного диска. Всё, что туда запишется, "
                    "ляжет на системный диск."
                )
            elif d.is_alert:
                problems[f"disk:{d.label}"] = (
                    f"💽 Диск «{esc(d.label)}» заполнен на {d.percent:.0f}% "
                    f"(свободно {human_bytes(d.free)})."
                )
        # Путь, который вообще не читается, get_disks молча пропускает
        for label, path in settings.disks:
            if label not in seen:
                problems[f"gone:{label}"] = (
                    f"💽 Диск «{esc(label)}» недоступен: {esc(path)} не читается."
                )
    except _Hung as e:
        _hung_problem(problems, e)
    except Exception:
        log.exception("Мониторинг: не удалось проверить диски")

    try:
        containers = await _guarded(
            "docker", lambda: asyncio.to_thread(docker_service.list_containers), 60
        )
        for c in containers:
            if c.is_problem:
                problems[f"container:{c.name}"] = (
                    f"🐳 Контейнер «{esc(c.name)}» в состоянии «{esc(c.status)}», "
                    f"хотя должен работать. Логи: /logs {esc(c.name)}"
                )
    except _Hung as e:
        _hung_problem(problems, e)
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
            # read_smart сам ограничен 60 с на диск, общий предел с запасом
            infos = await _guarded(
                "SMART", smart_service.read_all,
                (smart_service.SMART_TIMEOUT + 15) * len(settings.smart_device_list),
            )
            for info in infos:
                if info.error:
                    problems[f"smart:{info.device}"] = (
                        f"💽 SMART {esc(info.device)}: диск не читается — {esc(info.error)}"
                    )
                    continue
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
        except _Hung as e:
            _hung_problem(problems, e)
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

    if settings.vpn_check_port:
        try:
            # Стучимся из дома напрямую: если порт молчит отсюда, но сервер
            # при этом жив — значит адрес попал под блокировку у провайдера.
            # Это надо узнавать самому, а не из жалоб «у меня не работает».
            if await vpn_service.probe_port(settings.vpn_check_port):
                if _vpn_outage_start and _vpn_fail_cycles >= settings.vpn_confirm_fails:
                    _pending_reports.append(_vpn_outage_text(_vpn_outage_start, datetime.now()))
                    _vpn_report_queued = True
                _vpn_fail_cycles = 0
                _vpn_outage_start = None
            else:
                _vpn_fail_cycles += 1
                if _vpn_outage_start is None:
                    _vpn_outage_start = datetime.now()
                if _vpn_fail_cycles == 1:
                    log.warning("VPN-порт не ответил (цикл 1) — жду подтверждения")
            if _vpn_fail_cycles >= settings.vpn_confirm_fails:
                minutes = _vpn_fail_cycles * max(settings.monitor_interval_minutes, 1)
                problems["vpn:порт"] = (
                    f"🔐 VPN-сервер не отвечает из дома уже ~{minutes} мин. "
                    "Если сам сервер жив — скорее всего его адрес заблокировали. "
                    "Статус: /vpn. Переезд — на сервере: "
                    "deploy/vps-migrate.sh root@новый_адрес"
                )
            elif settings.vpn_sub_port and not await vpn_service.probe_port(
                settings.vpn_sub_port
            ):
                # Раздача подписок отдельным алертом: без неё люди не получат
                # новый адрес при переезде, даже если сам VPN работает
                problems["vpn:подписки"] = (
                    "🔐 Раздача подписок VPN не отвечает — при смене сервера "
                    "клиенты не смогут забрать новый адрес. Проверьте /vpn"
                )
        except Exception:
            log.exception("Мониторинг: не удалось проверить VPN")

    try:
        backup_status = await asyncio.to_thread(photo_backup.read_status)
        if backup_status:
            text = photo_backup.problem(backup_status)
            if text:
                problems["photo-backup:бэкап фото"] = text
    except Exception:
        log.exception("Мониторинг: не удалось проверить бэкап фото")

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


def _vpn_outage_text(start: datetime, end: datetime) -> str:
    fmt = "%d.%m %H:%M"
    return (
        "🔐 <b>VPN снова работает.</b> Сервер не отвечал из дома примерно "
        f"с {start:{fmt}} до {end:{fmt}} "
        f"(~{human_duration((end - start).total_seconds())}).\n"
        "Сообщить раньше было нельзя: бот ходит в Telegram через этот же сервер."
    )


async def monitor_check(bot: Bot) -> None:
    global _vpn_report_queued
    chat_id = settings.notify_chat_id
    if chat_id is None:
        return

    problems, oneoffs = await _collect_problems()
    _pending_reports.extend(oneoffs)

    new_keys = set(problems) - set(_active_alerts)
    resolved_keys = set(_active_alerts) - set(problems)

    # По одному и удаляем только после успешной отправки: если Telegram ещё
    # недоступен, исключение оставит остаток очереди до следующей проверки.
    while _pending_reports:
        try:
            await bot.send_message(chat_id, _pending_reports[0])
        except TelegramBadRequest:
            # Telegram отверг сам текст — повтор не поможет, а застрявшее
            # сообщение заблокировало бы очередь навсегда
            log.exception("Мониторинг: Telegram не принял сообщение, пропускаю")
        _pending_reports.pop(0)
    if _vpn_report_queued:
        # Отчёт о простое заменяет обычное «снова в порядке» для VPN
        resolved_keys.discard("vpn:порт")
        _active_alerts.pop("vpn:порт", None)
        _vpn_report_queued = False

    if new_keys:
        lines = ["🚨 <b>HomePilot: обнаружены проблемы</b>\n"]
        lines += [problems[k] for k in sorted(new_keys)]
        await bot.send_message(chat_id, "\n".join(lines))

    if resolved_keys:
        lines = ["✅ <b>HomePilot: проблемы устранены</b>\n"]
        lines += [f"• {esc(k.split(':', 1)[-1])} снова в порядке" for k in sorted(resolved_keys)]
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
            # Тяжёлый BDRemux лучше облегчить сразу, пока его никто не смотрит
            try:
                from app.handlers.lighten import offer_after_download

                await offer_after_download(bot, chat_id, t.id)
            except Exception:
                log.exception("Не удалось предложить облегчение для %s", t.name)
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


# Порог, ниже которого простой считаем штатной перезагрузкой. Метрики
# пишутся раз в несколько минут, поэтому даже у обычного ребута между
# последней точкой и загрузкой набегает несколько минут разрыва.
DOWNTIME_MIN_SECONDS = 10 * 60


async def downtime_report(bot: Bot) -> None:
    """После запуска сообщить, сколько сервера не было.

    Заменяет прежний детектор «выключили свет»: тот следил за переходом
    ноутбука на аккумулятор, а у нынешней машины батареи нет — при
    пропаже питания она гаснет мгновенно и предупредить не успевает.
    Поэтому отчитываемся задним числом: сравниваем время последней
    записи метрик с моментом загрузки хоста.
    """
    chat_id = settings.notify_chat_id
    if chat_id is None:
        return

    last = await asyncio.to_thread(metrics.last_ts)
    if last is None:
        return

    boot = psutil.boot_time()
    gap = boot - last
    # Отрицательный разрыв — перезапустился сам бот, хост при этом работал.
    if gap < DOWNTIME_MIN_SECONDS:
        return

    fmt = "%d.%m в %H:%M"
    text = (
        "⚡ <b>Сервер не работал " + human_duration(gap) + "</b>\n"
        "Пропал " + datetime.fromtimestamp(last).strftime(fmt) + ", "
        "поднялся " + datetime.fromtimestamp(boot).strftime(fmt) + ".\n\n"
        "Скорее всего отключали свет. Аккумулятора у сервера нет, "
        "поэтому сообщить в тот момент было некому."
    )

    # Закрываем разрыв сразу: иначе повторный запуск бота до первой
    # плановой записи метрик прислал бы тот же отчёт ещё раз. Текст уже
    # посчитан и живёт в этой задаче, пока не уйдёт.
    st = await system_service.get_status()
    await asyncio.to_thread(
        metrics.record, st.cpu_percent, st.ram_percent, st.cpu_temp
    )

    # После отключения света роутер и VPN-мост поднимаются позже сервера, и
    # первая попытка отправки обычно падает. Раньше она была единственной,
    # и отчёт терялся ровно в том случае, ради которого написан.
    delay = 30
    while True:
        try:
            await bot.send_message(chat_id, text)
            return
        except TelegramBadRequest:
            log.exception("Отчёт о простое: Telegram не принял текст")
            return
        except Exception as e:  # сетевые ошибки и ошибки socks-прокси
            log.warning("Отчёт о простое не ушёл: %s. Повтор через %d с.", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 600)
