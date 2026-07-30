"""Скачивание видео по ссылке через yt-dlp (YouTube, TikTok, Instagram, VK и др.).

yt-dlp вызывается как внешняя утилита — так проще ловить ошибки и не тянуть
её API в код. Для склейки видео+звука в образе нужен ffmpeg.
"""

import asyncio
import json
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>\"]+")

# Хосты, для которых предлагаем скачивание. yt-dlp умеет больше, но
# реагировать на любую ссылку в чате — плохая идея.
SUPPORTED_HOSTS = (
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "tiktok.com", "vm.tiktok.com",
    "instagram.com", "instagr.am",
    "vk.com", "vk.ru", "vkvideo.ru",
    "rutube.ru", "vimeo.com", "dailymotion.com",
    "twitter.com", "x.com",
    "reddit.com", "twitch.tv", "ok.ru", "coub.com",
)

# Хосты, которые у российских провайдеров не работают напрямую: Instagram
# режется по DNS, TikTok отдаёт нашим IP страницу, которую yt-dlp не парсит.
# Для них сразу идём через прокси, не тратя время на обречённую попытку.
PROXY_ONLY_HOSTS = (
    "tiktok.com", "instagram.com", "instagr.am",
    "facebook.com", "threads.net", "twitter.com", "x.com",
)

# Лимит Telegram на отправку файла ботом — 50 МБ; берём с запасом
TG_SIZE_LIMIT = 45 * 1024 * 1024
DOWNLOAD_TIMEOUT = 900  # 15 минут на скачивание


@dataclass
class MediaInfo:
    title: str
    duration: int | None
    uploader: str
    is_live: bool


@dataclass
class Downloaded:
    path: Path
    title: str
    size: int
    duration: int | None


def find_url(text: str) -> str | None:
    """Первая ссылка из сообщения, если хост поддерживается."""
    for match in _URL_RE.findall(text or ""):
        url = match.rstrip(").,;")
        if any(host in url.lower() for host in SUPPORTED_HOSTS):
            return url
    return None


async def _run(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise ServiceError(
            "yt-dlp не найден в контейнере — пересоберите образ: "
            "sudo docker compose up -d --build"
        ) from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ServiceError("Скачивание затянулось дольше 15 минут и было прервано.") from None
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def _base_args(use_proxy: bool) -> list[str]:
    args = ["--no-playlist", "--no-warnings", "--socket-timeout", "30", "--retries", "3"]
    if use_proxy and settings.ytdl_proxy:
        args += ["--proxy", settings.ytdl_proxy]
    return args


def needs_proxy(url: str) -> bool:
    """Сайт, который заведомо не открывается напрямую от нашего провайдера."""
    low = url.lower()
    return any(host in low for host in PROXY_ONLY_HOSTS)


async def _run_smart(
    build_args, url: str, timeout: int
) -> tuple[int, str, str]:
    """Запускает yt-dlp, при неудаче повторяя через прокси.

    Известно заблокированные сайты сразу идут через прокси; для остальных
    сначала пробуем напрямую (быстрее и не нагружает VPS), а если не вышло
    и прокси настроен — делаем вторую попытку через него.
    """
    via_proxy = needs_proxy(url) and bool(settings.ytdl_proxy)
    code, out, err = await _run(build_args(_base_args(via_proxy)), timeout)
    if code == 0 or via_proxy or not settings.ytdl_proxy:
        return code, out, err

    log.info("yt-dlp: прямая попытка не удалась, пробую через прокси (%s)", url)
    return await _run(build_args(_base_args(True)), timeout)


def _friendly_error(stderr: str) -> str:
    low = stderr.lower()
    if "sign in to confirm" in low or "bot" in low and "cookies" in low:
        return "Видео требует авторизации (YouTube просит подтвердить, что вы не робот)."
    if "private" in low or "login required" in low:
        return "Видео приватное или требует входа в аккаунт."
    if "unavailable" in low or "not available" in low:
        return "Видео недоступно (удалено или заблокировано по региону)."
    if "unsupported url" in low:
        return "Эта ссылка не поддерживается."
    if "timed out" in low or "timeout" in low or "connection" in low:
        return "Не удалось подключиться к сайту (возможно, блокировка). Попробуйте ещё раз."
    tail = stderr.strip().splitlines()[-1][:200] if stderr.strip() else "неизвестная ошибка"
    return f"yt-dlp не смог скачать: {tail}"


async def probe(url: str) -> MediaInfo:
    """Узнать название и длительность, не скачивая видео."""
    code, out, err = await _run_smart(
        lambda base: base + ["-J", "--skip-download", url], url, timeout=120
    )
    if code != 0:
        raise ServiceError(_friendly_error(err))
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise ServiceError("Не удалось разобрать ответ yt-dlp.") from None
    return MediaInfo(
        title=data.get("title") or "без названия",
        duration=data.get("duration"),
        uploader=data.get("uploader") or data.get("channel") or "",
        is_live=bool(data.get("is_live")),
    )


def _format_args(mode: str) -> list[str]:
    if mode == "audio":
        return ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    if mode == "chat":
        # 720p и h264/mp4 — так Telegram проигрывает видео сразу в чате
        return [
            "-f", "bv*[height<=720]+ba/b[height<=720]/b",
            "-S", "res:720,vcodec:h264,acodec:aac",
            "--merge-output-format", "mp4",
            "--max-filesize", str(TG_SIZE_LIMIT),
        ]
    # library — максимальное разумное качество
    return [
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
        "-S", "res:1080,vcodec:h264",
        "--merge-output-format", "mp4",
    ]


async def download(url: str, mode: str, dest_dir: Path | None = None) -> Downloaded:
    """Скачивает видео. mode: chat | library | audio.

    Для chat/audio файл кладётся во временный каталог (после отправки удаляется),
    для library — сразу в медиатеку.
    """
    tmp_dir: Path | None = None
    if dest_dir is None:
        tmp_dir = Path(tempfile.mkdtemp(prefix="ytdl-"))
        target = tmp_dir
    else:
        target = dest_dir
        target.mkdir(parents=True, exist_ok=True)

    template = str(target / "%(title).70s [%(id)s].%(ext)s")

    def build(base: list[str]) -> list[str]:
        return base + _format_args(mode) + [
            "--no-progress", "--print", "after_move:filepath", "-o", template, url,
        ]

    try:
        code, out, err = await _run_smart(build, url, timeout=DOWNLOAD_TIMEOUT)
        if code != 0:
            if "File is larger than max-filesize" in err or "max-filesize" in err:
                raise ServiceError(
                    "Видео слишком большое для отправки в Telegram (лимит 50 МБ). "
                    "Выберите «В медиатеку» — оно появится в Jellyfin."
                )
            raise ServiceError(_friendly_error(err))

        path = None
        for line in out.strip().splitlines():
            candidate = Path(line.strip())
            if candidate.is_file():
                path = candidate
        if path is None:  # на всякий случай ищем сами
            files = sorted(target.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
            path = next((f for f in files if f.is_file()), None)
        if path is None:
            raise ServiceError("Файл скачался, но не найден на диске.")

        size = path.stat().st_size
        if mode in ("chat", "audio") and size > TG_SIZE_LIMIT:
            raise ServiceError(
                f"Файл получился {size / 1024 / 1024:.0f} МБ — больше лимита Telegram (50 МБ). "
                "Попробуйте «В медиатеку»."
            )
        return Downloaded(
            path=path,
            title=path.stem,
            size=size,
            duration=None,
        )
    except Exception:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def cleanup(path: Path) -> None:
    """Удаляет временный каталог со скачанным файлом."""
    parent = path.parent
    if parent.name.startswith("ytdl-"):
        shutil.rmtree(parent, ignore_errors=True)
