"""Команды Jellyfin: /jellyfin_status, /movie."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from app.formatting import esc
from app.services.errors import ServiceError
from app.services.jellyfin import JellyfinClient, MediaItem

router = Router(name="jellyfin")


def _movie_caption(movie: MediaItem) -> str:
    lines = [f"🍿 <b>{esc(movie.name)}</b>" + (f" ({movie.year})" if movie.year else "")]
    details = []
    if movie.runtime_minutes:
        details.append(f"⏱ {movie.runtime_minutes} мин")
    if movie.rating:
        details.append(f"⭐ {movie.rating:.1f}")
    if movie.genres:
        details.append(esc(", ".join(movie.genres[:4])))
    if details:
        lines.append(" · ".join(details))
    if movie.overview:
        overview = movie.overview
        if len(overview) > 700:
            overview = overview[:700].rsplit(" ", 1)[0] + "…"
        lines.append(f"\n{esc(overview)}")
    return "\n".join(lines)


@router.message(Command("jellyfin_status"))
async def cmd_jellyfin_status(message: Message) -> None:
    try:
        jellyfin = JellyfinClient()
        info = await jellyfin.ping()
        lines = [
            f"🎬 <b>Jellyfin</b>: ✅ доступен "
            f"({esc(info.get('ServerName', ''))}, v{esc(info.get('Version', '?'))})"
        ]
        latest = await jellyfin.latest(limit=7)
        if latest:
            lines.append("\n🆕 <b>Последние добавления:</b>")
            for item in latest:
                lines.append(f"• {esc(item.title_line)}")
        await message.answer("\n".join(lines))
    except ServiceError as e:
        await message.answer(f"⚠️ {esc(e.user_message)}")


@router.message(Command("movie"))
async def cmd_movie(message: Message) -> None:
    await message.answer("🎲 Выбираю фильм на вечер…")
    try:
        jellyfin = JellyfinClient()
        movie = await jellyfin.random_movie()
        if movie is None:
            await message.answer("В медиатеке не нашлось ни одного фильма.")
            return
        caption = _movie_caption(movie)
        poster = await jellyfin.primary_image(movie.id)
        if poster:
            photo = BufferedInputFile(poster, filename="poster.jpg")
            await message.answer_photo(photo, caption=caption[:1024])
        else:
            await message.answer(caption)
    except ServiceError as e:
        await message.answer(f"⚠️ {esc(e.user_message)}")
