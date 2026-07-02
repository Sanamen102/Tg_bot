# 🏠 HomePilot

Личный Telegram-бот — «пульт управления» домашним сервером на Ubuntu.
Следит за сервером и Docker-контейнерами, дружит с Immich и Jellyfin,
присылает воспоминания из фотоархива и советует фильм на вечер.

Без сайта и приложений: весь интерфейс — Telegram.

## Возможности

- **Сервер:** uptime, CPU, RAM, swap, диски, предупреждения о заполненных дисках
- **Docker:** список контейнеров, логи, перезапуск (только из whitelist), отчёт «что сломалось»
- **Immich:** статус и статистика, случайное фото, «воспоминания» — фото этого дня в прошлые годы (только чтение, ничего не удаляет и не меняет)
- **Jellyfin:** статус, последние добавления, случайный фильм с постером и описанием
- **Сводки:** `/today` и `/week`, ежедневное воспоминание и еженедельный отчёт по расписанию
- **Мониторинг:** фоновая проверка дисков и контейнеров с алертами в Telegram
- **Детектор отключения света:** сервер-ноутбук перешёл на аккумулятор → мгновенный алерт «выключили свет», сеть вернулась → «свет дали», заряд упал ниже порога → критическое предупреждение
- **Безопасность:** бот отвечает только пользователям из `ALLOWED_USER_IDS`, перезапуск контейнеров ограничен whitelist-ом, секреты живут только в `.env`

## Команды

| Команда | Что делает |
|---|---|
| `/start`, `/help` | Справка |
| `/ping` | Проверка, что бот жив |
| `/status` | CPU, RAM, swap, uptime, диски |
| `/disk` | Подробно про свободное место |
| `/containers` | Контейнеры, статусы и «что сломалось» |
| `/logs имя [строк]` | Логи контейнера (по умолчанию 50 строк) |
| `/restart имя` | Перезапуск контейнера из whitelist, с подтверждением кнопкой |
| `/immich_status` | Доступность Immich + статистика фото/видео |
| `/memory` | Случайное фото из библиотеки |
| `/memory_today` | Фото/видео, снятые в этот день в прошлые годы |
| `/day [дата]` | Все фото за день — альбомами по 10 с кнопкой «Показать ещё» (`02.07.2024`, `2024-07-02`, `02.07`; без даты — сегодня) |
| `/jellyfin_status` | Доступность Jellyfin + последние добавления |
| `/movie` | Случайный фильм на вечер (постер, год, жанры, описание) |
| `/zapret` | Обход DPI: статус и вкл/выкл/перезапуск кнопками (если настроен) |
| `/today` | Сводка: сервер, контейнеры, Immich, Jellyfin |
| `/week` | Недельная сводка: новые фото, новые фильмы/серии, диски, проблемы |

## Быстрый старт (Ubuntu Server + Docker Compose)

### 1. Создайте бота и узнайте свой user_id

1. Напишите [@BotFather](https://t.me/BotFather) → `/newbot` → получите **токен**.
2. Напишите [@userinfobot](https://t.me/userinfobot) → получите свой **user_id**.

### 2. Получите API-ключи

- **Immich:** веб-интерфейс → аватар → *Account Settings* → *API Keys* → *New API Key*.
  Для статистики в `/immich_status` ключ должен принадлежать администратору; для фото и воспоминаний хватит обычного.
- **Jellyfin:** *Панель управления* → *Дополнительно* → *API-ключи* → `+`.

### 3. Настройте и запустите

```bash
git clone <ваш-репозиторий> homepilot   # или просто скопируйте папку на сервер
cd homepilot

cp .env.example .env
nano .env        # впишите токен, user_id, адреса и ключи

docker compose up -d --build
```

Проверьте, что бот поднялся:

```bash
docker compose logs -f homepilot
```

Напишите боту `/start` — должно прийти приветствие. Если пишет «Доступ запрещён» — проверьте `ALLOWED_USER_IDS`.

### Запуск без Docker (для разработки)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # и заполнить
python -m app.main
```

Без Docker бот видит метрики машины напрямую — укажите в `DISK_PATHS` реальные пути (например, `root:/`).

## Как это работает

- Бот работает в контейнере, но `/proc` внутри контейнера показывает метрики **хоста** (CPU, RAM, uptime) — отдельных агентов не нужно.
- Диски хоста видны через ро-монтирование `/:/host/root:ro`, поэтому пути в `DISK_PATHS` начинаются с `/host/root`.
- Docker управляется через сокет `/var/run/docker.sock`.
- Immich и Jellyfin опрашиваются по HTTP API, ключи передаются только в заголовках. Бот **ничего не пишет** в Immich/Jellyfin — только читает.
- Если какой-то сервис недоступен, бот не падает: соответствующая часть сводки заменяется понятным сообщением об ошибке.

## Безопасность

- **Whitelist пользователей:** `ALLOWED_USER_IDS` — всем остальным бот отвечает «Доступ запрещён» и пишет попытку в лог.
- **Whitelist перезапуска:** `/restart` работает только для контейнеров из `DOCKER_RESTART_WHITELIST`, и всегда просит подтверждение кнопкой.
- **Секреты:** токены и ключи живут в `.env` (в `.gitignore` и `.dockerignore`), в сообщения и логи не попадают.
- ⚠️ Монтирование `docker.sock` даёт контейнеру бота полный доступ к Docker хоста. Это осознанный компромисс для домашнего сервера: бот сам ничего не делает с контейнерами, кроме списка/логов/перезапуска по whitelist, но держите `.env` и сам сервер в безопасности.

## Если Telegram заблокирован провайдером

Симптом: контейнер запускается, но в логах `TelegramNetworkError: Request timeout error`,
при этом интернет на сервере работает. Проверка с хоста:

```bash
curl -m 10 -sS https://api.telegram.org/ -o /dev/null -w "%{http_code}\n"
```

Если таймаут — провайдер блокирует Telegram API. Боту нужен прокси в `.env`.
Прокси используется только для Telegram — Immich и Jellyfin ходят напрямую по локальной сети.

### Вариант 1: свой XRay/VLESS-сервер (Amnezia и т.п.) — рекомендуется

В compose уже есть сервис `xray` — клиент, который подключается к вашему серверу
по VLESS + Reality (маскируется под TLS, DPI его не режет) и отдаёт боту SOCKS5
внутри докерной сети.

1. Получите ссылку `vless://...` вашего сервера. В приложении AmneziaVPN:
   выберите сервер → «Поделиться» → протокол **XRay** → там будет ссылка вида:
   ```
   vless://UUID@1.2.3.4:443?security=reality&pbk=ПУБЛИЧНЫЙ_КЛЮЧ&fp=chrome&sni=САЙТ&sid=КОРОТКИЙ_ID&flow=xtls-rprx-vision#имя
   ```
2. Создайте конфиг клиента и перенесите значения из ссылки:
   ```bash
   cp xray/config.json.example xray/config.json
   nano xray/config.json
   ```
   Соответствие полей: `UUID` → `id`, адрес и порт → `address`/`port`,
   `pbk=` → `publicKey`, `sni=` → `serverName`, `sid=` → `shortId`,
   `fp=` → `fingerprint`. Если в ссылке нет `flow=` — удалите строку `"flow"` из конфига.
3. В `.env`: `TELEGRAM_PROXY=socks5://xray:1080`
4. `sudo docker compose up -d --build`, затем проверка с хоста:
   ```bash
   curl -m 10 --socks5-hostname 127.0.0.1:1080 -sS https://api.telegram.org/ -o /dev/null -w "%{http_code}\n"
   ```
   Любой HTTP-код (например, 302) = прокси работает. Таймаут = смотрите `sudo docker compose logs xray`.

Файл `xray/config.json` содержит ключи и находится в `.gitignore` — в репозиторий не попадает.

### Вариант 2: любой внешний SOCKS5/HTTP-прокси

```
TELEGRAM_PROXY=socks5://user:pass@адрес:1080
```

Подойдёт дешёвый зарубежный VPS с dante/3proxy. Обычный незамаскированный SOCKS5
до российского IP может быть заблокирован DPI так же, как сам Telegram — потому
вариант 1 надёжнее. MTProto-прокси **не подойдёт**: Bot API работает по HTTPS.

## Управление zapret (обход DPI) с бота

Если на хосте-шлюзе стоит [zapret](https://github.com/bol-van/zapret) как systemd-сервис,
бот умеет показывать его статус и включать/выключать/перезапускать командой `/zapret`
(inline-кнопки). Бот живёт в контейнере, zapret — на хосте, поэтому связь идёт по SSH
с **forced command**: ключ бота на хосте может выполнить только скрипт-обёртку, который
принимает строго `start/stop/restart/status`. Даже утёкший ключ не даёт shell на хосте.

Безопасно для самого бота: его доступ к Telegram идёт через xray/VLESS и от zapret
не зависит — выключив zapret, бот останется на связи и включит обратно.

### Настройка на хосте

```bash
# 1. Ключ (генерируем на хосте, кладём в проект — приватный ключ в .gitignore)
mkdir -p ~/Tg_bot/ssh
ssh-keygen -t ed25519 -f ~/Tg_bot/ssh/id_ed25519 -N "" -C homepilot-zapret
chmod 600 ~/Tg_bot/ssh/id_ed25519

# 2. Скрипт-обёртка
sudo install -m 755 ~/Tg_bot/deploy/zapret-ctl.sh /usr/local/bin/zapret-ctl

# 3. Разрешить пользователю управлять сервисом без пароля
#    (замените san на своего пользователя)
echo 'san ALL=(root) NOPASSWD: /usr/bin/systemctl start zapret, /usr/bin/systemctl stop zapret, /usr/bin/systemctl restart zapret' | sudo tee /etc/sudoers.d/homepilot-zapret
sudo chmod 440 /etc/sudoers.d/homepilot-zapret

# 4. Привязать ключ бота к forced command в authorized_keys
KEY=$(cat ~/Tg_bot/ssh/id_ed25519.pub)
echo "command=\"/usr/local/bin/zapret-ctl\",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty $KEY" >> ~/.ssh/authorized_keys
```

### Настройка в `.env`

```
ZAPRET_SSH_USER=san            # пользователь на хосте, чей authorized_keys правили
ZAPRET_SSH_HOST=host.docker.internal
ZAPRET_SSH_PORT=22
ZAPRET_SSH_KEY_PATH=/app/ssh/id_ed25519
```

`host.docker.internal` резолвится в IP хоста благодаря `extra_hosts` в compose.
После `docker compose up -d --build` проверьте `/zapret` в боте.
Если `ZAPRET_SSH_USER` пуст — функция и команда `/zapret` просто выключены.

## Настройка (все переменные .env)

| Переменная | Обязательна | Описание |
|---|---|---|
| `BOT_TOKEN` | ✅ | Токен от BotFather |
| `ALLOWED_USER_IDS` | ✅ | user_id через запятую |
| `ADMIN_CHAT_ID` | — | Куда слать плановые сообщения (по умолчанию первый из allowed) |
| `TELEGRAM_PROXY` | — | SOCKS5/HTTP-прокси для Telegram API, если он заблокирован |
| `DOCKER_RESTART_WHITELIST` | — | Контейнеры, разрешённые к перезапуску |
| `DISK_PATHS` | — | Диски для контроля: `метка:/путь,...` |
| `DISK_ALERT_THRESHOLD` | — | Порог алерта по диску, % (по умолчанию 90) |
| `IMMICH_URL`, `IMMICH_API_KEY` | — | Подключение к Immich |
| `JELLYFIN_URL`, `JELLYFIN_API_KEY` | — | Подключение к Jellyfin |
| `JELLYFIN_USER_ID` | — | От чьего имени смотреть медиатеку |
| `ZAPRET_SSH_USER` | — | Пользователь на хосте для управления zapret (пусто = выкл) |
| `ZAPRET_SSH_HOST` | — | Адрес хоста (по умолчанию host.docker.internal) |
| `ZAPRET_SSH_PORT` | — | Порт SSH хоста (по умолчанию 22) |
| `ZAPRET_SSH_KEY_PATH` | — | Путь к приватному ключу внутри контейнера |
| `DAILY_MEMORY_TIME` | — | Время «воспоминания дня», `HH:MM` (пусто = выкл) |
| `WEEKLY_REPORT_DAY`, `WEEKLY_REPORT_TIME` | — | Еженедельный отчёт (пустое время = выкл) |
| `MONITOR_INTERVAL_MINUTES` | — | Интервал мониторинга, мин (0 = выкл) |
| `POWER_CHECK_INTERVAL_SECONDS` | — | Проверка питания ноутбука, сек (0 = выкл) |
| `BATTERY_LOW_THRESHOLD` | — | Порог заряда для критического алерта, % (по умолчанию 25) |
| `TZ` | — | Часовой пояс для расписания |

Immich и Jellyfin можно не настраивать — соответствующие команды просто вежливо скажут, что сервис не сконфигурирован.

## Структура проекта

```
app/
├── main.py          # точка входа: бот, роутеры, планировщик
├── config.py        # настройки из .env
├── auth.py          # middleware: доступ только по whitelist
├── formatting.py    # человекочитаемые байты, время, даты
├── actions.py       # сводки /today, /week, отправка воспоминаний
├── monitor.py       # фоновые алерты (диски, контейнеры)
├── scheduler.py     # плановые задачи (APScheduler)
├── services/        # клиенты внешних систем
│   ├── system.py         # psutil: CPU/RAM/диски
│   ├── docker_service.py # Docker SDK
│   ├── immich.py         # Immich HTTP API (только чтение)
│   └── jellyfin.py       # Jellyfin HTTP API (только чтение)
└── handlers/        # Telegram-команды по модулям
    ├── basic.py, system.py, containers.py
    ├── immich.py, jellyfin.py, digest.py
```

## Как расширять

Новый модуль = новый сервис + новый роутер:

1. Клиент внешней системы → `app/services/мой_сервис.py` (кидает `ServiceError` с понятным текстом при проблемах).
2. Команды → `app/handlers/мой_модуль.py` с собственным `Router`.
3. Подключить роутер в `app/main.py` (`dp.include_routers(...)`) и добавить команду в `BOT_COMMANDS`.
4. Если нужен кусочек в сводке `/today` — добавить функцию в `app/actions.py`.

Идеи для следующих версий: управление systemd-сервисами, торрент-клиент, бэкапы,
speedtest, wake-on-lan для других устройств, отправка видео из Immich целиком.
