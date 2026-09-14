#!/usr/bin/env python3
"""Видео из альбома Immich -> жёсткие ссылки в медиатеке Jellyfin.

Залил видео с камеры в Immich и положил в альбом — оно само появляется
на телевизоре. Второй копии на диске не возникает: жёсткая ссылка это
второе имя для тех же данных.

Если в медиатеке уже лежит отдельная копия того же видео, скрипт сверяет
её контрольную сумму с той, что хранит Immich, и при совпадении заменяет
копию ссылкой — дубль схлопывается сам, место освобождается.

Работает на хосте (а не в контейнере бота), потому что ссылку можно
создать только в пределах одной точки монтирования: библиотека Immich
и медиатека видны боту как разные маунты (EXDEV).
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

# Альбомов может быть несколько — через запятую. Раньше поддерживался
# ровно один, и добавление второго молча выключало первый.
ALBUM_NAMES = [a.strip() for a in os.environ.get('SYNC_ALBUM', 'Sony').split(',') if a.strip()]
DEST = Path(os.environ.get('SYNC_DEST', '/mnt/storage/media/home'))
ENV_FILE = Path('/home/san/Tg_bot/.env')
STATE = Path('/var/lib/immich-jellyfin-sync.json')

# путь внутри контейнера immich -> путь на хосте
CONTAINER_PREFIX = '/usr/src/app/upload'
# Пути идут через /mnt/storage, а не через /home/san: это одна и та же
# файловая система, но /home/san/media и /home/san/immich-app —
# отдельные bind-монтирования, а ядро не даёт делать жёсткие ссылки
# через границу точки монтирования. Внутри /mnt/storage граница одна.
HOST_PREFIX = '/mnt/storage/immich-app/library'


def read_env() -> dict:
    env = {}
    for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()
    return env


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def notify(env: dict, text: str) -> None:
    token = env.get('BOT_TOKEN')
    chat = (env.get('ALLOWED_USER_IDS') or '').split(',')[0].strip()
    if not token or not chat:
        return
    try:
        subprocess.run(
            ['curl', '-s', '--max-time', '15', '--socks5-hostname', '127.0.0.1:1080',
             '-d', f'chat_id={chat}', '--data-urlencode', f'text={text}',
             f'https://api.telegram.org/bot{token}/sendMessage'],
            capture_output=True, timeout=25)
    except Exception as e:
        print(f'уведомление не ушло: {e}')


def api_post(url: str, key: str, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f'{url}/api{path}',
        headers={'x-api-key': key, 'Content-Type': 'application/json'},
        data=json.dumps(payload).encode(), method='POST')
    return json.load(urllib.request.urlopen(req, timeout=90))


def api_get(url: str, key: str, path: str):
    req = urllib.request.Request(f'{url}/api{path}', headers={'x-api-key': key})
    return json.load(urllib.request.urlopen(req, timeout=60))


def safe_name(name: str) -> str:
    name = Path(name).name
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip() or 'video.mp4'


def sha1_of(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def immich_checksum_hex(asset: dict) -> str | None:
    """Immich отдаёт SHA1 в base64 — переводим в hex для сравнения."""
    cs = asset.get('checksum')
    if not cs:
        return None
    try:
        return base64.b64decode(cs).hex()
    except Exception:
        return None


def jellyfin_refresh(env: dict) -> bool:
    """Просим Jellyfin пересканировать библиотеку, чтобы видео появилось сразу."""
    url = (env.get('JELLYFIN_URL') or '').rstrip('/')
    key = env.get('JELLYFIN_API_KEY')
    if not url or not key:
        return False
    try:
        # Jellyfin 12 отключил старую авторизацию: X-Emby-Token отвечает 401,
        # и пересканирование молча не запускалось.
        req = urllib.request.Request(f'{url}/Library/Refresh', method='POST',
                                     headers={'Authorization': f'MediaBrowser Token="{key}"'},
                                     data=b'')
        urllib.request.urlopen(req, timeout=30)
        return True
    except Exception as e:
        print(f'Jellyfin не отозвался на пересканирование: {e}')
        return False


def link_over(src: Path, dst: Path) -> None:
    """Атомарно подменяет dst жёсткой ссылкой на src."""
    tmp = dst.parent / (dst.name + '.sync-tmp')
    try:
        if tmp.exists():
            tmp.unlink()
        os.link(src, tmp)
        os.replace(tmp, dst)
    except OSError:
        if tmp.exists():
            tmp.unlink()
        raise


def main() -> int:
    env = read_env()
    url = (env.get('IMMICH_URL') or '').rstrip('/')
    key = env.get('IMMICH_API_KEY')
    if not url or not key:
        print('нет IMMICH_URL/IMMICH_API_KEY в .env')
        return 1

    state = load_state()
    # файлы, у которых содержимое реально другое — чтобы не пересчитывать
    # их контрольные суммы при каждом запуске
    mismatched = set(state.get('mismatched', []))

    albums = api_get(url, key, '/albums')
    found = [a for a in albums if a['albumName'] in ALBUM_NAMES]
    missing = [n for n in ALBUM_NAMES if not any(a['albumName'] == n for a in found)]
    if missing:
        # Не падаем из-за одного опечатанного имени: остальные альбомы
        # синхронизировать всё равно надо.
        print('не найдены альбомы: ' + ', '.join(f'«{m}»' for m in missing))
    if not found:
        return 1

    # видео альбома (в v3 список файлов достаётся поиском, не из самого альбома)
    # Каждый альбом опрашиваем отдельно: Immich понимает список albumIds
    # как пересечение, а не объединение. Передать два сразу — значит
    # получить только видео, лежащие в обоих, то есть обычно пустоту.
    videos, seen = [], set()
    for alb in found:
        page = 1
        while True:
            res = api_post(url, key, '/search/metadata', {
                'albumIds': [alb['id']], 'type': 'VIDEO', 'size': 500, 'page': page,
            })
            items = res.get('assets', {}).get('items', [])
            for it in items:
                # Одно видео может лежать в нескольких альбомах —
                # ссылку на него делаем один раз.
                if it.get('id') not in seen:
                    seen.add(it.get('id'))
                    videos.append(it)
            if len(items) < 500:
                break
            page += 1

    DEST.mkdir(parents=True, exist_ok=True)
    created, merged, errors = [], [], []
    skipped = 0

    for asset in videos:
        src_raw = asset.get('originalPath') or ''
        if not src_raw:
            continue
        src = Path(src_raw.replace(CONTAINER_PREFIX, HOST_PREFIX, 1))
        dst = DEST / safe_name(asset.get('originalFileName') or src.name)

        if not src.is_file():
            errors.append(f'{dst.name}: исходник не найден')
            continue

        if not dst.exists():
            try:
                link_over(src, dst)
                created.append((dst.name, src.stat().st_size))
            except OSError as e:
                errors.append(f'{dst.name}: {e.strerror}')
            continue

        # файл с таким именем уже есть
        try:
            if dst.stat().st_ino == src.stat().st_ino:
                skipped += 1        # уже одна и та же ссылка, всё хорошо
                continue
        except OSError as e:
            errors.append(f'{dst.name}: {e.strerror}')
            continue

        if dst.name in mismatched:
            skipped += 1            # уже знаем, что содержимое другое
            continue

        # отдельная копия: если содержимое идентично — схлопываем в ссылку
        size = dst.stat().st_size
        if size != src.stat().st_size:
            mismatched.add(dst.name)
            errors.append(f'{dst.name}: другой файл с тем же именем, не трогаю')
            continue

        want = immich_checksum_hex(asset)
        if not want:
            errors.append(f'{dst.name}: Immich не отдал контрольную сумму, пропускаю')
            continue

        print(f'  сверяю копию {dst.name} ({size/1024**3:.2f} ГБ)…')
        if sha1_of(dst) != want:
            mismatched.add(dst.name)
            errors.append(f'{dst.name}: другой файл с тем же именем, не трогаю')
            continue

        try:
            link_over(src, dst)
            merged.append((dst.name, size))
        except OSError as e:
            errors.append(f'{dst.name}: {e.strerror}')

    freed = sum(s for _, s in merged)
    print(f'альбомы: {', '.join(a['albumName'] for a in found)} — видео {len(videos)}, новых ссылок {len(created)}, '
          f'схлопнуто дублей {len(merged)}, уже было {skipped}, ошибок {len(errors)}')
    for name, size in created:
        print(f'  + {size/1024**3:.2f} ГБ  {name}')
    for name, size in merged:
        print(f'  = {size/1024**3:.2f} ГБ  {name} (освобождено)')
    for e in errors:
        print(f'  ! {e}')

    if created or merged:
        refreshed = jellyfin_refresh(env) if created else False
        lines = []
        if created:
            lines.append(f'🎬 В медиатеку добавлено видео: {len(created)} шт.')
            for name, size in created[:8]:
                lines.append(f'  • {name} ({size/1024**3:.2f} ГБ)')
            if len(created) > 8:
                lines.append(f'  …и ещё {len(created) - 8}')
            lines.append('Места не занято — файлы общие с Immich.')
        if merged:
            lines.append(f'♻️ Схлопнуто дублей: {len(merged)} — освобождено {freed/1024**3:.1f} ГБ.')
            for name, size in merged[:5]:
                lines.append(f'  • {name} ({size/1024**3:.2f} ГБ)')
        if refreshed:
            lines.append('Jellyfin уже обновляет библиотеку.')
        notify(env, '\n'.join(lines))

    STATE.write_text(json.dumps({
        'albums': [a['albumName'] for a in found], 'videos': len(videos),
        'linked_now': len(created), 'merged_now': len(merged),
        'mismatched': sorted(mismatched),
    }, ensure_ascii=False), encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
