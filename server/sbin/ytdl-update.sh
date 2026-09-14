#!/usr/bin/env bash
# Держит загрузчики бота свежими.
#
# YouTube меняет подпись медиа-ссылок раз в несколько недель: устаревший yt-dlp
# ещё читает метаданные, но сами потоки уже отдают 403. То же и с gallery-dl —
# Instagram и TikTok правят разметку. Ждать, пока пользователь заметит поломку,
# смысла нет, поэтому обновляемся сами.
#
# ВАЖНО: pip ставит пакеты ВНУТРЬ работающего контейнера, и при пересборке
# (docker compose up --build) они откатятся к версиям из образа. Поэтому нижняя
# граница в requirements.txt тоже поднята — она страхует этот случай.
set -uo pipefail

C=homepilot
ENV_FILE=/home/san/Tg_bot/.env
# Метку кладёт бот, пока облегчает фильм (LIGHTEN_LOCK_PATH, том data/)
LIGHTEN_LOCK=/home/san/Tg_bot/data/lighten.lock

notify() {
    local token chat
    token=$(grep -m1 '^BOT_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)
    chat=$(grep -m1 '^ALLOWED_USER_IDS=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | cut -d, -f1)
    if [ -n "${token:-}" ] && [ -n "${chat:-}" ]; then
        curl -s --max-time 15 --socks5-hostname 127.0.0.1:1080 \
            -d "chat_id=$chat" --data-urlencode "text=$1" \
            "https://api.telegram.org/bot${token}/sendMessage" >/dev/null 2>&1 || true
    fi
}

if ! docker inspect -f '{{.State.Running}}' "$C" 2>/dev/null | grep -q true; then
    echo "контейнер $C не запущен — пропускаю"
    exit 0
fi

# Перезапуск убьёт пересборку фильма, которая идёт полчаса и дольше, — ради
# обновления yt-dlp это слишком дорого. Обновимся при следующем запуске таймера.
if [ -f "$LIGHTEN_LOCK" ]; then
    echo "бот облегчает фильм ($(cut -d' ' -f2- "$LIGHTEN_LOCK")) — обновление отложено"
    exit 0
fi

before_yt=$(docker exec "$C" yt-dlp --version 2>/dev/null || echo "?")
before_gd=$(docker exec "$C" gallery-dl --version 2>/dev/null || echo "?")

if ! docker exec "$C" pip install -q -U --no-cache-dir yt-dlp gallery-dl >/dev/null 2>&1; then
    echo "обновить не удалось (сеть?) — оставляю прежние версии"
    exit 0
fi

after_yt=$(docker exec "$C" yt-dlp --version 2>/dev/null || echo "?")
after_gd=$(docker exec "$C" gallery-dl --version 2>/dev/null || echo "?")

changed=""
if [ "$before_yt" != "$after_yt" ]; then
    changed="yt-dlp $before_yt → $after_yt"
fi
if [ "$before_gd" != "$after_gd" ]; then
    if [ -n "$changed" ]; then changed="$changed, "; fi
    changed="${changed}gallery-dl $before_gd → $after_gd"
fi

if [ -z "$changed" ]; then
    echo "уже свежие: yt-dlp $after_yt, gallery-dl $after_gd"
    exit 0
fi

# Между проверкой метки и этим местом могла начаться пересборка
if [ -f "$LIGHTEN_LOCK" ]; then
    echo "обновлено: $changed, но бот начал облегчать фильм — перезапуск отложен"
    notify "🔄 Обновлены загрузчики: $changed. Бот не перезапущен: идёт облегчение фильма — новые версии заработают после следующего перезапуска."
    exit 0
fi

echo "обновлено: $changed — перезапускаю бота"
docker restart "$C" >/dev/null 2>&1
notify "🔄 Обновлены загрузчики: $changed. Бот перезапущен."
