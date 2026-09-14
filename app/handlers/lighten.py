"""Облегчение тяжёлых фильмов: /heavy, /originals и предложение после закачки.

Дорожки выбираются кнопками: бот сам отмечает лучшую русскую и лучшую
оригинальную, пользователь может поправить. Путь к файлу и выбор не влезают
в callback_data (лимит 64 байта), поэтому живут в памяти по короткому токену —
как ссылки в ytdl.py и magnet-ссылки в torrents.py. После перезапуска бота
старые кнопки скажут, что устарели; отложенные оригиналы при этом не теряются —
их показывает /originals.
"""

import asyncio
import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.formatting import esc, human_bytes, human_duration, progress_bar
from app.services import lighten
from app.services.errors import ServiceError
from app.services.jellyfin import JellyfinClient
from app.services.transmission import TransmissionClient

log = logging.getLogger(__name__)
router = Router(name="lighten")

BUTTON_TEXT_MAX = 60
TRACK_BUTTONS_MAX = 95  # Telegram: не больше 100 кнопок в одном сообщении
HEAVY_SHOWN = 15
ORIGINALS_SHOWN = 20

DISABLED_TEXT = (
    "Облегчение фильмов не настроено: задайте LIGHTEN_DIRS в .env "
    "(например <code>/media/movie,/media/show</code>)."
)


@dataclass
class Selection:
    info: lighten.MediaInfo
    keep: set[int]
    heading: str = ""


_files: OrderedDict[str, Path] = OrderedDict()                       # /heavy -> файл
_selections: OrderedDict[str, Selection] = OrderedDict()            # выбор дорожек
_stored: OrderedDict[str, tuple[Path | None, Path]] = OrderedDict()  # (новый файл, оригинал)
_tasks: set[asyncio.Task] = set()  # держим ссылки на задачи, иначе их может собрать GC


def _remember(store: OrderedDict, value, limit: int = 50) -> str:
    token = secrets.token_urlsafe(6)
    store[token] = value
    while len(store) > limit:
        store.popitem(last=False)
    return token


def _short(text: str) -> str:
    return text if len(text) <= BUTTON_TEXT_MAX else text[: BUTTON_TEXT_MAX - 1] + "…"


def _selection_view(token: str, sel: Selection) -> tuple[str, InlineKeyboardMarkup]:
    info = sel.info
    lines = [sel.heading, ""] if sel.heading else []
    lines.append(f"🪶 <b>{esc(info.path.name)}</b>")
    lines.append(
        f"{human_bytes(info.size)} · {human_duration(info.duration)} · "
        f"{info.bitrate / 1e6:.0f} Мбит/с · дорожек звука {len(info.audio)}, "
        f"субтитров {len(info.subtitles)}"
    )
    estimate = info.estimate_size(sel.keep)
    if estimate is not None:
        lines.append(
            f"\nПосле облегчения: ≈ <b>{human_bytes(estimate)}</b> "
            f"(легче на {human_bytes(info.size - estimate)})"
        )
    else:
        lines.append("\nРазмер после облегчения не оценить: в файле нет статистики дорожек.")
    lines.append("Отметьте, что оставить. Видео остаётся всегда, ничего не перекодируется.")

    tracks = [("🔊", t) for t in info.audio] + [("💬", t) for t in info.subtitles]
    rows = []
    for icon, track in tracks[:TRACK_BUTTONS_MAX]:
        mark = "✅" if track.index in sel.keep else "▫️"
        rows.append([
            InlineKeyboardButton(
                text=_short(f"{mark} {icon} {track.label()}"),
                callback_data=f"lt:t:{token}:{track.index}",
            )
        ])
    if len(tracks) > TRACK_BUTTONS_MAX:
        lines.append(
            f"(кнопок не больше {TRACK_BUTTONS_MAX}: остальные субтитры "
            "останутся, как отмечено по умолчанию)"
        )
    rows.append([
        InlineKeyboardButton(text="▶️ Облегчить", callback_data=f"lt:g:{token}"),
        InlineKeyboardButton(text="↩️ Как советую", callback_data=f"lt:r:{token}"),
    ])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=f"lt:x:{token}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _stored_keyboard(token: str, final: Path | None) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text="🗑 Удалить оригинал", callback_data=f"lt:d:{token}")]
    if final is not None:
        row.append(InlineKeyboardButton(text="↩️ Вернуть оригинал", callback_data=f"lt:u:{token}"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


async def _refresh_jellyfin() -> bool:
    if not settings.jellyfin_url:
        return False
    try:
        return await JellyfinClient().refresh_library()
    except ServiceError:
        return False


async def _stop_torrent(path: Path) -> bool:
    """Раздавать изменённый файл нельзя — Transmission будет сыпать ошибками проверки."""
    if not settings.transmission_url:
        return False
    try:
        torrent_id = await lighten.torrent_for_path(path)
        if torrent_id is None:
            return False
        await TransmissionClient().stop([torrent_id])
        return True
    except ServiceError as e:
        log.warning("Не удалось остановить раздачу %s: %s", path.name, e.user_message)
        return False


# ---------- Команды ----------

@router.message(Command("heavy"))
async def cmd_heavy(message: Message) -> None:
    if not settings.lighten_enabled:
        await message.answer(DISABLED_TEXT)
        return
    wait = await message.answer("🔍 Ищу тяжёлые фильмы — читаю только заголовки файлов…")
    try:
        items = await lighten.find_heavy()
    except ServiceError as e:
        await wait.edit_text(f"⚠️ {esc(e.user_message)}")
        return
    if not items:
        await wait.edit_text(
            "👌 Облегчать нечего: ни в одном файле от "
            f"{settings.lighten_min_file_gb:g} ГБ лишние дорожки не занимают больше "
            f"{settings.lighten_min_savings_gb:g} ГБ."
        )
        return

    total = sum(saving for _, saving in items)
    lines = [f"🪶 <b>Можно облегчить</b> — всего станет легче на ≈ {human_bytes(total)}\n"]
    rows = []
    for info, saving in items[:HEAVY_SHOWN]:
        token = _remember(_files, info.path)
        lines.append(
            f"• <b>{esc(info.path.name)}</b>\n"
            f"  {human_bytes(info.size)}, дорожек звука {len(info.audio)} → "
            f"легче на ≈ {human_bytes(saving)}"
        )
        rows.append([
            InlineKeyboardButton(text=_short(f"🪶 {info.path.stem}"), callback_data=f"lt:o:{token}")
        ])
    if len(items) > HEAVY_SHOWN:
        lines.append(f"\n…и ещё {len(items) - HEAVY_SHOWN}.")
    lines.append("\nНажмите на фильм, чтобы выбрать дорожки.")
    current = lighten.busy()
    if current:
        lines.append(f"\n⏳ Сейчас облегчаю «{esc(current)}».")
    await wait.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(Command("originals"))
async def cmd_originals(message: Message) -> None:
    if not settings.lighten_enabled:
        await message.answer(DISABLED_TEXT)
        return
    items = await asyncio.to_thread(lighten.list_originals)
    if not items:
        await message.answer("🗄 Отложенных оригиналов нет — место уже освобождено.")
        return
    total = sum(size for _, size in items)
    await message.answer(
        f"🗄 <b>Отложенные оригиналы</b>: {len(items)} шт., {human_bytes(total)}.\n"
        "Jellyfin их не видит. Удаляйте, когда убедитесь, что облегчённые версии в порядке."
    )
    for path, size in items[:ORIGINALS_SHOWN]:
        token = _remember(_stored, (None, path))
        await message.answer(
            f"• <b>{esc(path.name)}</b> — {human_bytes(size)}\n"
            f"<code>{esc(str(path.parent.parent))}</code>",
            reply_markup=_stored_keyboard(token, None),
        )


async def offer_after_download(bot: Bot, chat_id: int, torrent_id: int) -> None:
    """Если докачался тяжёлый фильм — сразу прислать выбор дорожек."""
    if not settings.lighten_enabled:
        return
    for path in await lighten.torrent_videos(torrent_id):
        try:
            info = await lighten.cached_probe(path)
        except ServiceError as e:
            log.warning("Облегчение после закачки: %s", e.user_message)
            continue
        keep = lighten.recommended(info)
        estimate = info.estimate_size(keep)
        if estimate is None or info.size - estimate < settings.lighten_min_savings_gb * lighten.GB:
            continue
        sel = Selection(
            info,
            keep,
            heading=(
                "💡 <b>Фильм тяжёлый</b>: лишние дорожки занимают ≈ "
                f"{human_bytes(info.size - estimate)}. Облегчить?"
            ),
        )
        token = _remember(_selections, sel, limit=20)
        text, keyboard = _selection_view(token, sel)
        await bot.send_message(chat_id, text, reply_markup=keyboard)


# ---------- Кнопки ----------

async def _stale(callback: CallbackQuery, msg: Message) -> None:
    await callback.answer("Кнопка устарела — повторите команду.", show_alert=True)
    try:
        await msg.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


async def _open(callback: CallbackQuery, msg: Message, token: str, _rest: list[str]) -> None:
    path = _files.get(token)
    if path is None:
        await _stale(callback, msg)
        return
    await callback.answer("Читаю дорожки…")
    info = await lighten.cached_probe(path)
    sel = Selection(info, lighten.recommended(info))
    stoken = _remember(_selections, sel, limit=20)
    text, keyboard = _selection_view(stoken, sel)
    await msg.answer(text, reply_markup=keyboard)


async def _toggle(callback: CallbackQuery, msg: Message, token: str, rest: list[str]) -> None:
    sel = _selections.get(token)
    if sel is None:
        await _stale(callback, msg)
        return
    try:
        index = int(rest[0])
    except (IndexError, ValueError):
        await callback.answer()
        return
    sel.keep ^= {index}
    await callback.answer()
    text, keyboard = _selection_view(token, sel)
    await msg.edit_text(text, reply_markup=keyboard)


async def _recommend(callback: CallbackQuery, msg: Message, token: str, _rest: list[str]) -> None:
    sel = _selections.get(token)
    if sel is None:
        await _stale(callback, msg)
        return
    sel.keep = lighten.recommended(sel.info)
    await callback.answer()
    text, keyboard = _selection_view(token, sel)
    try:
        await msg.edit_text(text, reply_markup=keyboard)
    except Exception:
        pass  # выбор и так совпадал с рекомендованным — Telegram отвечает «not modified»


async def _cancel(callback: CallbackQuery, msg: Message, token: str, _rest: list[str]) -> None:
    _selections.pop(token, None)
    await callback.answer()
    await msg.edit_text("Облегчение отменено, файл не тронут.")


async def _go(callback: CallbackQuery, msg: Message, token: str, _rest: list[str]) -> None:
    sel = _selections.get(token)
    if sel is None:
        await _stale(callback, msg)
        return
    if not any(t.index in sel.keep for t in sel.info.audio):
        await callback.answer("Оставьте хотя бы одну звуковую дорожку.", show_alert=True)
        return
    current = lighten.busy()
    if current:
        await callback.answer(f"Уже облегчаю «{current[:120]}» — дождитесь окончания.", show_alert=True)
        return
    _selections.pop(token, None)
    await callback.answer("Запускаю")
    await msg.edit_text(f"⏳ Облегчаю <b>{esc(sel.info.path.name)}</b>…", reply_markup=None)
    task = asyncio.create_task(_run(callback.bot, msg.chat.id, msg.message_id, sel))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _run(bot: Bot, chat_id: int, message_id: int, sel: Selection) -> None:
    info = sel.info
    name = esc(info.path.name)
    started = time.monotonic()
    titles = {
        "remux": "⏳ Облегчаю",
        "verify": "🔎 Сверяю с оригиналом",
        "install": "📦 Кладу в медиатеку",
    }

    async def edit(text: str) -> None:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
        except Exception:
            pass  # «message is not modified» или сеть — не повод бросать работу

    async def on_progress(stage: str, fraction: float, written: int) -> None:
        elapsed = time.monotonic() - started
        lines = [f"{titles.get(stage, stage)} <b>{name}</b>"]
        if stage != "verify":
            lines.append(f"{progress_bar(fraction * 100)} {fraction * 100:.0f}%")
            details = []
            if written:
                details.append(f"записано {human_bytes(written)}")
            details.append(f"прошло {human_duration(elapsed)}")
            if stage == "remux" and fraction > 0.02:
                details.append(f"осталось ~{human_duration(elapsed * (1 - fraction) / fraction)}")
            lines.append(" · ".join(details))
        await edit("\n".join(lines))

    try:
        final, stored, new_size = await lighten.run_job(info, sel.keep, on_progress)
    except ServiceError as e:
        await edit(f"⚠️ Не получилось облегчить <b>{name}</b>: {esc(e.user_message)}")
        return
    except Exception:
        log.exception("Облегчение %s упало", info.path)
        await edit(
            f"⚠️ Внутренняя ошибка при облегчении <b>{name}</b>, подробности в логах бота. "
            "Оригинал не удалён: он на прежнем месте или в .originals."
        )
        return

    torrent_stopped = await _stop_torrent(info.path)
    refreshed = await _refresh_jellyfin()
    token = _remember(_stored, (final, stored))
    lines = [
        f"✅ <b>Облегчил {name}</b>",
        f"{human_bytes(info.size)} → {human_bytes(new_size)} "
        f"за {human_duration(time.monotonic() - started)}",
        "",
        "Оригинал отложен в папку <code>.originals</code> рядом с фильмом — Jellyfin его "
        "не видит. Проверьте новый файл и удалите оригинал кнопкой: место "
        f"({human_bytes(info.size)}) освободится только тогда.",
    ]
    if torrent_stopped:
        lines.append("⏸ Раздачу торрента остановил: файл теперь другой, раздавать его нельзя.")
    lines.append(
        "🎬 Jellyfin уже пересканирует библиотеку."
        if refreshed
        else "Jellyfin подхватит файл при ближайшем сканировании."
    )
    await edit(f"✅ Готово: <b>{name}</b>")
    await bot.send_message(chat_id, "\n".join(lines), reply_markup=_stored_keyboard(token, final))


async def _ask_delete(callback: CallbackQuery, msg: Message, token: str, _rest: list[str]) -> None:
    item = _stored.get(token)
    if item is None:
        await _stale(callback, msg)
        return
    try:
        size = item[1].stat().st_size
    except OSError:
        size = 0
    await callback.answer()
    await msg.edit_reply_markup(
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"🗑 Да, удалить {human_bytes(size)}", callback_data=f"lt:D:{token}"),
            InlineKeyboardButton(text="Не надо", callback_data=f"lt:k:{token}"),
        ]])
    )


async def _delete(callback: CallbackQuery, msg: Message, token: str, _rest: list[str]) -> None:
    item = _stored.pop(token, None)
    if item is None:
        await _stale(callback, msg)
        return
    freed = await asyncio.to_thread(lighten.delete_original, item[1])
    await callback.answer("Удалено")
    await msg.edit_text(
        f"{msg.html_text}\n\n🗑 Оригинал удалён, освобождено {human_bytes(freed)}.",
        reply_markup=None,
    )


async def _keep(callback: CallbackQuery, msg: Message, token: str, _rest: list[str]) -> None:
    item = _stored.get(token)
    if item is None:
        await _stale(callback, msg)
        return
    await callback.answer()
    await msg.edit_reply_markup(reply_markup=_stored_keyboard(token, item[0]))


async def _ask_restore(callback: CallbackQuery, msg: Message, token: str, _rest: list[str]) -> None:
    item = _stored.get(token)
    if item is None or item[0] is None:
        await _stale(callback, msg)
        return
    await callback.answer()
    await msg.edit_reply_markup(
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="↩️ Да, вернуть оригинал", callback_data=f"lt:U:{token}"),
            InlineKeyboardButton(text="Не надо", callback_data=f"lt:k:{token}"),
        ]])
    )


async def _restore(callback: CallbackQuery, msg: Message, token: str, _rest: list[str]) -> None:
    item = _stored.pop(token, None)
    if item is None or item[0] is None:
        await _stale(callback, msg)
        return
    final, stored = item
    await asyncio.to_thread(lighten.restore, final, stored)
    refreshed = await _refresh_jellyfin()
    await callback.answer("Вернул")
    note = " Jellyfin уже пересканирует библиотеку." if refreshed else ""
    await msg.edit_text(
        f"{msg.html_text}\n\n↩️ Оригинал вернул на место, облегчённая копия удалена.{note}",
        reply_markup=None,
    )


_ACTIONS = {
    "o": _open,
    "t": _toggle,
    "r": _recommend,
    "x": _cancel,
    "g": _go,
    "d": _ask_delete,
    "D": _delete,
    "k": _keep,
    "u": _ask_restore,
    "U": _restore,
}


@router.callback_query(F.data.startswith("lt:"))
async def on_callback(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    action = _ACTIONS.get(parts[1]) if len(parts) >= 3 else None
    msg = callback.message
    if action is None or not isinstance(msg, Message):
        await callback.answer("Кнопка устарела — повторите команду.", show_alert=True)
        return
    try:
        await action(callback, msg, parts[2], parts[3:])
    except ServiceError as e:
        await callback.answer(e.user_message[:190], show_alert=True)
    except Exception:
        log.exception("Кнопка облегчения %s", callback.data)
        await callback.answer("Не получилось — подробности в логах бота.", show_alert=True)
