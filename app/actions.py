"""Общие действия: сводки /today и /week, отправка воспоминаний и случайных фото.

Используются и хендлерами команд, и планировщиком.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta

import httpx
from aiogram import Bot
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)

from app.config import settings
from app.formatting import esc, human_bytes, human_duration, ru_date, ru_years_ago
from app.services import docker_service
from app.services import system as system_service
from app.services import zapret as zapret_service
from app.services.errors import ServiceError
from app.services.immich import Asset, ImmichClient
from app.services.jellyfin import JellyfinClient
from app.services.transmission import TransmissionClient

log = logging.getLogger(__name__)


# ---------- Кусочки сводок ----------

async def _server_summary() -> str:
    st = await system_service.get_status()
    lines = [
        f"🖥 <b>Сервер:</b> CPU {st.cpu_percent:.0f}% · RAM {st.ram_percent:.0f}% · "
        f"аптайм {human_duration(st.uptime_seconds)}"
    ]
    for d in st.disks:
        mark = " ⚠️" if d.is_alert else ""
        lines.append(
            f"💽 {esc(d.label)}: {d.percent:.0f}% "
            f"(свободно {human_bytes(d.free)}){mark}"
        )
    for problem in st.problems:
        lines.append(f"⚠️ {esc(problem)}")
    return "\n".join(lines)


async def _docker_summary() -> str:
    containers = await asyncio.to_thread(docker_service.list_containers)
    running = sum(1 for c in containers if c.status == "running")
    line = f"🐳 <b>Контейнеры:</b> {running}/{len(containers)} запущено"
    problems = [c for c in containers if c.is_problem]
    if problems:
        names = ", ".join(f"{c.emoji} {esc(c.name)}" for c in problems)
        line += f"\n⚠️ Проблемы: {names}"
    return line


async def _immich_summary() -> str:
    immich = ImmichClient()
    if not await immich.ping():
        return "📸 <b>Immich:</b> ❌ не отвечает"
    stats = await immich.statistics()
    if stats:
        return (
            f"📸 <b>Immich:</b> ✅ доступен · фото: {stats.get('photos', '?')} · "
            f"видео: {stats.get('videos', '?')}"
        )
    return "📸 <b>Immich:</b> ✅ доступен"


async def _jellyfin_summary() -> str:
    jellyfin = JellyfinClient()
    info = await jellyfin.ping()
    return f"🎬 <b>Jellyfin:</b> ✅ доступен (v{esc(info.get('Version', '?'))})"


async def _zapret_summary() -> str:
    active = await zapret_service.is_active()
    return "🛡 <b>Zapret:</b> " + ("✅ активен" if active else "❌ выключен")


async def _watch_summary() -> str:
    parts = []
    for label, url in settings.watch_services:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.get(url)
            ok = resp.status_code < 500
        except httpx.HTTPError:
            ok = False
        parts.append(f"{'✅' if ok else '❌'} {esc(label)}")
    return "🌐 <b>Сервисы:</b> " + " · ".join(parts)


async def _transmission_summary() -> str:
    torrents = await TransmissionClient().torrents()
    downloading = [t for t in torrents if t.status == 4]
    seeding = sum(1 for t in torrents if t.status == 6)
    line = f"⬇️ <b>Transmission:</b> качается {len(downloading)}, раздаётся {seeding}"
    for t in downloading[:3]:
        line += f"\n  • {esc(t.name[:50])} — {t.percent * 100:.0f}%"
    return line


async def _safe(coro, fallback_prefix: str) -> str:
    try:
        return await coro
    except ServiceError as e:
        return f"{fallback_prefix} ⚠️ {esc(e.user_message)}"
    except Exception:
        log.exception("Ошибка при сборке сводки (%s)", fallback_prefix)
        return f"{fallback_prefix} ⚠️ внутренняя ошибка, подробности в логах бота"


# ---------- Сводка "сегодня" ----------

async def build_today_text() -> str:
    tasks = [
        _safe(_server_summary(), "🖥 <b>Сервер:</b>"),
        _safe(_docker_summary(), "🐳 <b>Контейнеры:</b>"),
        _safe(_immich_summary(), "📸 <b>Immich:</b>"),
        _safe(_jellyfin_summary(), "🎬 <b>Jellyfin:</b>"),
    ]
    if settings.zapret_enabled:
        tasks.append(_safe(_zapret_summary(), "🛡 <b>Zapret:</b>"))
    if settings.transmission_url:
        tasks.append(_safe(_transmission_summary(), "⬇️ <b>Transmission:</b>"))
    if settings.watch_services:
        tasks.append(_safe(_watch_summary(), "🌐 <b>Сервисы:</b>"))
    parts = await asyncio.gather(*tasks)
    today = datetime.now()
    header = f"🏠 <b>HomePilot — сводка на {ru_date(today)}</b>\n"
    return "\n\n".join([header, *parts])


# ---------- Сводка "неделя" ----------

async def _immich_week() -> str:
    immich = ImmichClient()
    since = datetime.now() - timedelta(days=7)
    count = await immich.count_created_since(since)
    suffix = "+" if count >= 1000 else ""
    return f"📸 <b>Immich:</b> новых фото/видео за неделю: {count}{suffix}"


async def _jellyfin_week() -> str:
    jellyfin = JellyfinClient()
    items = await jellyfin.latest(limit=30)
    cutoff = datetime.now() - timedelta(days=7)
    fresh = [i for i in items if i.date_created and i.date_created >= cutoff]
    movies = sum(1 for i in fresh if i.item_type == "Movie")
    episodes = sum(1 for i in fresh if i.item_type == "Episode")
    lines = [f"🎬 <b>Jellyfin:</b> за неделю добавлено — фильмов: {movies}, серий: {episodes}"]
    for item in fresh[:5]:
        lines.append(f"  • {esc(item.title_line)}")
    return "\n".join(lines)


async def build_week_text() -> str:
    parts = await asyncio.gather(
        _safe(_server_summary(), "🖥 <b>Сервер:</b>"),
        _safe(_docker_summary(), "🐳 <b>Контейнеры:</b>"),
        _safe(_immich_week(), "📸 <b>Immich:</b>"),
        _safe(_jellyfin_week(), "🎬 <b>Jellyfin:</b>"),
    )
    header = "🗓 <b>HomePilot — сводка за неделю</b>\n"
    return "\n\n".join([header, *parts])


# ---------- Immich: отправка фото ----------

def _asset_caption(asset: Asset, prefix: str = "") -> str:
    kind = "🎥 Видео" if asset.is_video else "📸 Фото"
    when = f" · {ru_date(asset.taken_at)}" if asset.taken_at else ""
    note = " (превью)" if asset.is_video else ""
    return f"{prefix}{kind}{note}{when}"


async def send_asset(bot: Bot, chat_id: int, asset: Asset, caption: str) -> None:
    immich = ImmichClient()
    data = await immich.thumbnail(asset.id)
    photo = BufferedInputFile(data, filename=f"{asset.id}.jpg")
    await bot.send_photo(chat_id, photo, caption=caption)


async def send_random_asset(bot: Bot, chat_id: int) -> None:
    immich = ImmichClient()
    assets = await immich.random_assets(1)
    if not assets:
        await bot.send_message(chat_id, "Библиотека Immich пуста — нечего показать.")
        return
    asset = assets[0]
    await send_asset(bot, chat_id, asset, _asset_caption(asset, "🎲 Случайное: "))


DAY_BATCH = 10   # фото в одном альбоме (лимит Telegram на media group)
DAY_MAX = 200    # максимум фото за день, которые вообще запрашиваем


async def send_day_photos(bot: Bot, chat_id: int, day: date, offset: int = 0) -> None:
    """Фото за конкретный день порциями по DAY_BATCH с кнопкой «Показать ещё»."""
    immich = ImmichClient()
    assets = await immich.assets_taken_on(day, size=DAY_MAX)
    total = len(assets)
    if total == 0:
        await bot.send_message(chat_id, f"За {ru_date(day)} фото не нашлось.")
        return
    batch = assets[offset : offset + DAY_BATCH]
    if not batch:
        await bot.send_message(chat_id, "Больше фото за этот день нет.")
        return

    thumbs = await asyncio.gather(
        *(immich.thumbnail(a.id) for a in batch), return_exceptions=True
    )
    end = offset + len(batch)
    total_str = f"{total}+" if total >= DAY_MAX else str(total)
    caption = f"📅 <b>{ru_date(day)}</b> — фото {offset + 1}–{end} из {total_str}"
    if any(a.is_video for a in batch):
        caption += "\n🎥 — превью видео"

    media: list[InputMediaPhoto] = []
    for asset, data in zip(batch, thumbs):
        if isinstance(data, BaseException):
            log.warning("Не удалось скачать превью %s", asset.id)
            continue
        # Подпись только у первого фото — тогда Telegram показывает её
        # как общую подпись всего альбома
        media.append(
            InputMediaPhoto(
                media=BufferedInputFile(data, filename=f"{asset.id}.jpg"),
                caption=caption if not media else None,
            )
        )
    if not media:
        await bot.send_message(chat_id, "⚠️ Immich не отдал ни одного превью, попробуйте ещё раз.")
        return

    if len(media) == 1:
        await bot.send_photo(chat_id, media[0].media, caption=caption)
    else:
        await bot.send_media_group(chat_id, media)

    if end < total:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"📷 Показать ещё (осталось {total - end})",
                        callback_data=f"day:{day.isoformat()}:{end}",
                    )
                ]
            ]
        )
        await bot.send_message(
            chat_id, f"Показано {end} из {total_str}.", reply_markup=keyboard
        )


async def send_memories_today(bot: Bot, chat_id: int, limit: int = 4) -> None:
    """Присылает фото, сделанные в этот день в прошлые годы. Если их нет — случайное."""
    immich = ImmichClient()
    memories = await immich.memories_today()
    if not memories:
        await bot.send_message(
            chat_id,
            "В этот день в прошлые годы фото не нашлось. Вот случайное из архива:",
        )
        await send_random_asset(bot, chat_id)
        return

    sent = 0
    for years_ago, assets in memories:
        for asset in assets:
            if sent >= limit:
                return
            when = asset.taken_at
            date_str = ru_date(when) if when else "дата неизвестна"
            caption = f"🕰 <b>{date_str}</b> — {ru_years_ago(years_ago)}\n{_asset_caption(asset)}"
            try:
                await send_asset(bot, chat_id, asset, caption)
                sent += 1
            except ServiceError as e:
                log.warning("Не удалось отправить воспоминание: %s", e.user_message)
