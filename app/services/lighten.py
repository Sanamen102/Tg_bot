"""Облегчение тяжёлых фильмов: выкинуть лишние дорожки без перекодирования.

UHD BDRemux'ы нередко везут десятки звуковых дорожек. У «Терминатора» (1984)
их было 42, и звук весил больше видео: 81 ГБ против 50. Смотрят одну, но
дорожки в контейнере перемешаны, поэтому при воспроизведении диск читает все —
для старого 2,5-дюймового диска это граница между «играет» и «заикается».

Ничего не перекодируется: ffmpeg копирует выбранные дорожки как есть
(-c copy), качество то же, процессор почти не занят — всё упирается в диски.

Порядок работы, от которого зависит сохранность файла:
1. Результат пишется в рабочую папку на ДРУГОМ диске. Если писать рядом с
   оригиналом, mergerfs положит файл на тот же физический диск, и чтение с
   записью будут драться за головки.
2. Готовый файл сверяется с оригиналом: длительность и число дорожек.
3. Копия заносится в папку фильма под скрытым именем, и только потом оригинал
   переименовывается в .originals/, а копия — на его место. Переименование
   внутри одного тома mergerfs мгновенное, копировать оригинал не нужно.
4. Оригинал сам не удаляется. В .originals лежит файл .ignore — Jellyfin туда
   не смотрит (проверено на Jellyfin 12), удаляет его пользователь кнопкой.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import stat
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.services.errors import ServiceError
from app.services.transmission import TransmissionClient

log = logging.getLogger(__name__)

GB = 1024 ** 3
VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".m2ts", ".ts", ".avi", ".mov", ".webm"}
ORIGINALS_DIR = ".originals"
PART_SUFFIX = ".lighten-part"
WORK_SUFFIX = ".lighten.mkv"
PROGRESS_EVERY = 20  # секунд между обновлениями прогресса: чаще Telegram не любит
PROBE_TIMEOUT = 120

_RUSSIAN = {"rus", "ru", "russian"}
_ORIGINAL_RE = re.compile(r"original|оригинал", re.IGNORECASE)
_DUB_RE = re.compile(r"\bdub\b|дубл", re.IGNORECASE)
_EXTRA_RE = re.compile(r"comment|коммент|descript|тифло", re.IGNORECASE)
_CHANNELS = {1: "1.0", 2: "2.0", 3: "2.1", 6: "5.1", 7: "6.1", 8: "7.1"}
_CODEC_NAMES = {
    "subrip": "SRT",
    "hdmv_pgs_subtitle": "PGS",
    "dvd_subtitle": "VobSub",
    "eac3": "E-AC3",
    "truehd": "TrueHD",
}
_BORING_PROFILES = {"", "unknown", "lc", "main", "main 10", "high"}

# (стадия remux/verify/install, доля 0..1, записано байт)
Progress = Callable[[str, float, int], Awaitable[None]]


@dataclass
class Track:
    index: int
    kind: str  # video / audio / subtitle / attachment / cover / data
    codec: str
    profile: str = ""
    language: str = ""
    title: str = ""
    channels: int = 0
    default: bool = False
    nbytes: int | None = None
    bps: int | None = None

    @property
    def is_russian(self) -> bool:
        return self.language in _RUSSIAN

    def label(self) -> str:
        """Короткое описание дорожки для кнопки."""
        profile = "" if self.profile.lower() in _BORING_PROFILES else self.profile
        codec = profile or _CODEC_NAMES.get(self.codec, self.codec.upper())
        codec = codec.replace("Dolby TrueHD + Dolby Atmos", "TrueHD Atmos")
        parts = [self.language or "—", codec]
        if self.kind == "audio" and self.channels:
            parts.append(_CHANNELS.get(self.channels, f"{self.channels}ch"))
        text = " ".join(parts)
        if self.nbytes and self.nbytes >= GB // 10:
            text += f" · {self.nbytes / GB:.1f} ГБ"
        if self.title:
            text += f" · {self.title}"
        return text


@dataclass
class MediaInfo:
    path: Path
    size: int
    duration: float
    tracks: list[Track]

    @property
    def audio(self) -> list[Track]:
        return [t for t in self.tracks if t.kind == "audio"]

    @property
    def subtitles(self) -> list[Track]:
        return [t for t in self.tracks if t.kind == "subtitle"]

    @property
    def main_video(self) -> Track | None:
        return next((t for t in self.tracks if t.kind == "video"), None)

    @property
    def bitrate(self) -> float:
        return self.size * 8 / self.duration if self.duration else 0.0

    def track_bytes(self, track: Track) -> int | None:
        if track.nbytes is not None:
            return track.nbytes
        if track.bps and self.duration:
            return int(track.bps * self.duration / 8)
        return None

    def estimate_size(self, keep: set[int]) -> int | None:
        """Сколько будет весить файл с дорожками keep (главное видео остаётся всегда).

        None — если у видео или звука нет статистики и оценить честно нельзя.
        """
        video = self.main_video
        kept = known = 0
        for track in self.tracks:
            if track.kind not in ("video", "audio", "subtitle"):
                continue
            size = self.track_bytes(track)
            if size is None:
                if track.kind == "subtitle":
                    continue  # субтитры весят килобайты — на оценку не влияют
                return None
            known += size
            if track is video or track.index in keep:
                kept += size
        # Разницу между файлом и суммой дорожек (контейнер, главы, вложения) оставляем
        return kept + max(0, self.size - known)


def _tag(tags: dict, name: str) -> str | None:
    # mkvmerge пишет статистику как BPS или BPS-eng — берём любой вариант
    for key, value in tags.items():
        upper = key.upper()
        if upper == name or upper.startswith(name + "-"):
            return value
    return None


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def probe(path: Path) -> MediaInfo:
    """Дорожки файла. Читаются только заголовки, поэтому это быстро даже на медленном диске."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise ServiceError("ffprobe не найден в контейнере — пересоберите образ.") from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise ServiceError(f"ffprobe не ответил за {PROBE_TIMEOUT} с на {path.name}.") from None
    if proc.returncode != 0:
        tail = err.decode(errors="replace").strip().splitlines()
        reason = tail[-1][:200] if tail else "ошибка ffprobe"
        raise ServiceError(f"Не удалось прочитать {path.name}: {reason}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise ServiceError(f"ffprobe вернул непонятный ответ на {path.name}.") from None
    return parse_probe(path, data)


def parse_probe(path: Path, data: dict) -> MediaInfo:
    """Разбор JSON от ffprobe -show_format -show_streams."""
    tracks = []
    for s in data.get("streams", []):
        tags = s.get("tags") or {}
        disposition = s.get("disposition") or {}
        kind = s.get("codec_type") or "data"
        if kind == "video" and disposition.get("attached_pic"):
            kind = "cover"
        tracks.append(
            Track(
                index=s["index"],
                kind=kind,
                codec=s.get("codec_name") or "?",
                profile=s.get("profile") or "",
                language=(tags.get("language") or "").lower(),
                title=tags.get("title") or "",
                channels=s.get("channels") or 0,
                default=bool(disposition.get("default")),
                nbytes=_int(_tag(tags, "NUMBER_OF_BYTES")),
                bps=_int(_tag(tags, "BPS")) or _int(s.get("bit_rate")),
            )
        )
    fmt = data.get("format") or {}
    try:
        duration = float(fmt.get("duration") or 0)
    except ValueError:
        duration = 0.0
    size = _int(fmt.get("size")) or path.stat().st_size
    return MediaInfo(path=path, size=size, duration=duration, tracks=tracks)


def _richest(info: MediaInfo, tracks: list[Track]) -> Track:
    return max(tracks, key=lambda t: (t.channels, info.track_bytes(t) or 0))


def recommended(info: MediaInfo) -> set[int]:
    """Что оставить по умолчанию: главное видео, лучшую русскую, лучшую оригинальную, все субтитры.

    Русская — дубляж, а не флаг «по умолчанию»: в сборниках озвучек флаг часто
    стоит на первой по списку дорожке. У «Терминатора» это была телевизионная
    закадровая MVO (ОРТ) в 2.0, а дубляж BD CEE в 5.1 шёл вторым.
    Среди дубляжей флагу доверяем — там его ставит сам релизер.
    """
    keep: set[int] = set()
    if info.main_video:
        keep.add(info.main_video.index)
    audio = [t for t in info.audio if not _EXTRA_RE.search(t.title)] or info.audio
    russian = [t for t in audio if t.is_russian]
    foreign = [t for t in audio if not t.is_russian]
    if russian:
        dubs = [t for t in russian if _DUB_RE.search(t.title)]
        if dubs:
            best = next((t for t in dubs if t.default), None) or _richest(info, dubs)
        else:
            best = next((t for t in russian if t.default), None) or _richest(info, russian)
        keep.add(best.index)
    if foreign:
        originals = [t for t in foreign if _ORIGINAL_RE.search(t.title)] or foreign
        keep.add(_richest(info, originals).index)
    keep.update(t.index for t in info.subtitles)
    return keep


# ---------- Медиатека ----------

def library_dirs() -> list[Path]:
    return [Path(d) for d in settings.lighten_dir_list]


def in_library(path: Path) -> bool:
    return any(path.is_relative_to(root) for root in library_dirs())


def _video_files(min_size: int) -> list[Path]:
    found = []
    for root in library_dirs():
        for dirpath, dirnames, filenames in os.walk(root):
            # скрытые папки, в том числе .originals, не трогаем
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                path = Path(dirpath, name)
                if name.startswith(".") or path.suffix.lower() not in VIDEO_EXTS:
                    continue
                try:
                    if path.stat().st_size >= min_size:
                        found.append(path)
                except OSError:
                    continue
    return found


_probe_cache: dict[str, tuple[int, float, MediaInfo]] = {}


async def cached_probe(path: Path) -> MediaInfo:
    try:
        st = await asyncio.to_thread(path.stat)
    except OSError:
        raise ServiceError(f"Файл {path.name} не найден — возможно, его переместили.") from None
    hit = _probe_cache.get(str(path))
    if hit and hit[0] == st.st_size and hit[1] == st.st_mtime:
        return hit[2]
    info = await probe(path)
    _probe_cache[str(path)] = (st.st_size, st.st_mtime, info)
    return info


async def find_heavy() -> list[tuple[MediaInfo, int]]:
    """Файлы, где лишние дорожки занимают больше LIGHTEN_MIN_SAVINGS_GB. Самые тяжёлые — первыми."""
    files = await asyncio.to_thread(_video_files, int(settings.lighten_min_file_gb * GB))
    heavy = []
    for path in files:
        try:
            info = await cached_probe(path)
        except ServiceError as e:
            log.warning("Облегчение: пропускаю %s — %s", path, e.user_message)
            continue
        estimate = info.estimate_size(recommended(info))
        if estimate is None:
            continue
        saving = info.size - estimate
        if saving >= settings.lighten_min_savings_gb * GB:
            heavy.append((info, saving))
    return sorted(heavy, key=lambda item: -item[1])


# ---------- Связь с Transmission ----------

def container_path(transmission_path: str) -> Path | None:
    """Путь, как его видит Transmission (/downloads/...) -> путь внутри бота (/media/...)."""
    pair = settings.torrent_path_pair
    if pair is None:
        return None
    src, dst = pair
    if transmission_path == src or transmission_path.startswith(src + "/"):
        return Path(dst + transmission_path[len(src):])
    return None


async def torrent_videos(torrent_id: int) -> list[Path]:
    """Крупные видеофайлы торрента, которые лежат в медиатеке."""
    min_size = int(settings.lighten_min_file_gb * GB)
    result = []
    for torrent in await TransmissionClient().torrent_files([torrent_id]):
        for rel, length in torrent.files:
            path = container_path(f"{torrent.download_dir.rstrip('/')}/{rel}")
            if (
                path is not None
                and length >= min_size
                and path.suffix.lower() in VIDEO_EXTS
                and in_library(path)
            ):
                result.append(path)
    return result


async def torrent_for_path(path: Path) -> int | None:
    for torrent in await TransmissionClient().torrent_files():
        for rel, _ in torrent.files:
            if container_path(f"{torrent.download_dir.rstrip('/')}/{rel}") == path:
                return torrent.id
    return None


# ---------- Сама операция ----------

# Одна пересборка за раз: две параллельные положат диски
_job_lock = asyncio.Lock()
_current: str | None = None


def busy() -> str | None:
    """Имя файла, который сейчас облегчается, или None."""
    return _current


async def _report(progress: Progress | None, stage: str, fraction: float, written: int) -> None:
    if progress is None:
        return
    try:
        await progress(stage, fraction, written)
    except Exception:
        log.exception("Не удалось показать прогресс облегчения")


async def run_job(
    info: MediaInfo, keep: set[int], progress: Progress | None = None
) -> tuple[Path, Path, int]:
    """Облегчает файл. Возвращает (новый файл, отложенный оригинал, размер нового файла)."""
    global _current
    if _job_lock.locked():
        raise ServiceError(f"Уже облегчаю «{_current}» — дождитесь окончания.")
    async with _job_lock:
        _current = info.path.name
        lock = Path(settings.lighten_lock_path)
        work_file = Path(settings.lighten_work_dir) / (info.path.stem + WORK_SUFFIX)
        try:
            # Метку видит ytdl-update.sh на хосте и не перезапускает бота посреди работы
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text(f"{os.getpid()} {info.path}\n", encoding="utf-8")
            await _remux(info, keep, work_file, progress)
            await _report(progress, "verify", 1.0, 0)
            new = await _verify(info, keep, work_file)
            final, stored = await _install(info, work_file, progress)
            return final, stored, new.size
        finally:
            work_file.unlink(missing_ok=True)
            lock.unlink(missing_ok=True)
            _current = None


async def _remux(info: MediaInfo, keep: set[int], out: Path, progress: Progress | None) -> None:
    video = info.main_video
    if video is None:
        raise ServiceError("В файле нет видеодорожки.")
    audio = [t for t in info.audio if t.index in keep]
    if not audio:
        raise ServiceError("Не выбрано ни одной звуковой дорожки.")
    audio.sort(key=lambda t: not t.is_russian)  # русская — первой и по умолчанию
    subtitles = [t for t in info.subtitles if t.index in keep]

    out.parent.mkdir(parents=True, exist_ok=True)
    estimate = info.estimate_size(keep) or info.size
    free = shutil.disk_usage(out.parent).free
    if free < estimate + GB:
        raise ServiceError(
            f"В рабочей папке мало места: нужно ≈ {estimate / GB:.0f} ГБ, "
            f"свободно {free / GB:.0f} ГБ."
        )

    args = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-nostats",
        "-progress", "pipe:1", "-i", str(info.path),
    ]
    for track in (video, *audio, *subtitles):
        args += ["-map", f"0:{track.index}"]
    # вложения (шрифты для ASS-субтитров) переносим, если они есть
    args += ["-map", "0:t?", "-c", "copy", "-map_metadata", "0", "-map_chapters", "0"]
    for n in range(len(audio)):
        args += [f"-disposition:a:{n}", "default" if n == 0 else "0"]
    args.append(str(out))

    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stderr_tail: deque[str] = deque(maxlen=20)

    async def drain_stderr() -> None:
        async for line in proc.stderr:
            stderr_tail.append(line.decode(errors="replace").rstrip())

    drain = asyncio.create_task(drain_stderr())
    written = 0
    last = 0.0
    try:
        async for raw in proc.stdout:
            key, _, value = raw.decode(errors="replace").strip().partition("=")
            if key == "total_size" and value.isdigit():
                written = int(value)
            elif key == "out_time_us" and value.isdigit() and info.duration:
                now = time.monotonic()
                if now - last >= PROGRESS_EVERY:
                    last = now
                    fraction = min(int(value) / 1e6 / info.duration, 1.0)
                    await _report(progress, "remux", fraction, written)
        await proc.wait()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    finally:
        await drain
    if proc.returncode != 0:
        tail = next((line for line in reversed(stderr_tail) if line), "неизвестная ошибка")
        raise ServiceError(f"ffmpeg завершился с ошибкой: {tail[:300]}")


async def _verify(info: MediaInfo, keep: set[int], out: Path) -> MediaInfo:
    new = await probe(out)
    expected = {
        "video": 1,
        "audio": sum(1 for t in info.audio if t.index in keep),
        "subtitle": sum(1 for t in info.subtitles if t.index in keep),
    }
    got = {kind: sum(1 for t in new.tracks if t.kind == kind) for kind in expected}
    if got != expected:
        raise ServiceError(
            "В новом файле не те дорожки: "
            f"видео {got['video']} из {expected['video']}, "
            f"звук {got['audio']} из {expected['audio']}, "
            f"субтитры {got['subtitle']} из {expected['subtitle']}. Оригинал не тронут."
        )
    if info.duration and abs(new.duration - info.duration) > max(2.0, info.duration * 0.002):
        raise ServiceError(
            f"Длительность не совпала: {new.duration:.0f} с против {info.duration:.0f} с "
            "у оригинала. Оригинал не тронут."
        )
    return new


async def _copy_with_progress(src: Path, dst: Path, progress: Progress | None) -> None:
    total = src.stat().st_size
    copy = asyncio.create_task(asyncio.to_thread(shutil.copyfile, src, dst))
    try:
        while True:
            done, _ = await asyncio.wait({copy}, timeout=PROGRESS_EVERY)
            if done:
                break
            try:
                written = dst.stat().st_size
            except OSError:
                written = 0
            await _report(progress, "install", written / total if total else 0.0, written)
        await copy
    except BaseException:
        dst.unlink(missing_ok=True)
        raise


async def _install(info: MediaInfo, out: Path, progress: Progress | None) -> tuple[Path, Path]:
    src = info.path
    if not src.is_file():
        raise ServiceError("Оригинал пропал, пока шла пересборка, — ничего не меняю.")
    lib_dir = src.parent
    final = src.with_suffix(".mkv")
    if final != src and final.exists():
        final = lib_dir / f"{src.stem}.lightened.mkv"

    size = out.stat().st_size
    free = shutil.disk_usage(lib_dir).free
    if free < size + GB:
        raise ServiceError(
            f"В медиатеке мало места для облегчённой копии: нужно {size / GB:.0f} ГБ, "
            f"свободно {free / GB:.0f} ГБ. Оригинал не тронут."
        )

    part = lib_dir / f".{final.name}{PART_SUFFIX}"
    await _copy_with_progress(out, part, progress)
    st = src.stat()
    try:
        os.chown(part, st.st_uid, st.st_gid)
        os.chmod(part, stat.S_IMODE(st.st_mode))
    except OSError as e:
        log.warning("Не удалось перенести владельца и права на %s: %s", part, e)

    originals = lib_dir / ORIGINALS_DIR
    originals.mkdir(exist_ok=True)
    (originals / ".ignore").touch(exist_ok=True)
    stored = originals / src.name
    if stored.exists():
        stored = originals / f"{src.stem}.{int(time.time())}{src.suffix}"

    try:
        os.rename(src, stored)
    except OSError as e:
        part.unlink(missing_ok=True)
        raise ServiceError(f"Не удалось отложить оригинал: {e.strerror}. Ничего не изменено.") from e
    try:
        os.rename(part, final)
    except OSError as e:
        try:
            os.rename(stored, src)
        except OSError:
            raise ServiceError(
                f"Сбой при замене файла: {e.strerror}. Оригинал лежит в {stored}, копия — в {part}."
            ) from e
        part.unlink(missing_ok=True)
        raise ServiceError(f"Сбой при замене файла: {e.strerror}. Оригинал возвращён на место.") from e
    return final, stored


# ---------- Отложенные оригиналы ----------

def list_originals() -> list[tuple[Path, int]]:
    result = []
    for root in library_dirs():
        for dirpath, dirnames, filenames in os.walk(root):
            if Path(dirpath).name == ORIGINALS_DIR:
                for name in filenames:
                    if name.startswith("."):
                        continue
                    path = Path(dirpath, name)
                    try:
                        result.append((path, path.stat().st_size))
                    except OSError:
                        continue
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d == ORIGINALS_DIR or not d.startswith(".")]
    return sorted(result, key=lambda item: -item[1])


def _check_stored(stored: Path) -> None:
    if stored.parent.name != ORIGINALS_DIR or not in_library(stored) or not stored.is_file():
        raise ServiceError("Оригинал не найден среди отложенных — возможно, он уже удалён.")


def _tidy_originals(folder: Path) -> None:
    try:
        if all(p.name == ".ignore" for p in folder.iterdir()):
            shutil.rmtree(folder)
    except OSError:
        pass


def delete_original(stored: Path) -> int:
    """Удаляет отложенный оригинал по кнопке пользователя. Возвращает освобождённые байты."""
    _check_stored(stored)
    size = stored.stat().st_size
    stored.unlink()
    _tidy_originals(stored.parent)
    return size


def restore(final: Path, stored: Path) -> Path:
    """Возвращает оригинал на место, облегчённая копия удаляется."""
    _check_stored(stored)
    target = stored.parent.parent / stored.name
    if final.is_file():
        final.unlink()
    if target.exists():
        raise ServiceError(f"На месте оригинала уже лежит {target.name} — не трогаю.")
    os.rename(stored, target)
    _tidy_originals(stored.parent)
    return target


def startup_cleanup() -> None:
    """После перезапуска бота прерванная пересборка не продолжится — убираем её следы."""
    Path(settings.lighten_lock_path).unlink(missing_ok=True)
    work = Path(settings.lighten_work_dir)
    if work.is_dir():
        for leftover in work.glob(f"*{WORK_SUFFIX}"):
            log.info("Облегчение: удаляю недописанный %s", leftover.name)
            leftover.unlink(missing_ok=True)
    for root in library_dirs():
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if name.endswith(PART_SUFFIX):
                    log.info("Облегчение: удаляю недокопированный %s", name)
                    Path(dirpath, name).unlink(missing_ok=True)
