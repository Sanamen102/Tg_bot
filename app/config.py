"""Конфигурация HomePilot: все настройки читаются из переменных окружения / .env."""

from functools import cached_property

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Telegram ---
    bot_token: str
    # Список разрешённых Telegram user_id через запятую: "123456,789012"
    allowed_user_ids: str
    # Куда слать плановые уведомления (по умолчанию — первый из allowed_user_ids)
    admin_chat_id: int | None = None
    # Прокси для доступа к Telegram API, если провайдер его блокирует.
    # Примеры: socks5://user:pass@host:1080, http://host:3128
    telegram_proxy: str = ""

    # --- Docker ---
    # Контейнеры, которые бот имеет право перезапускать: "immich_server,jellyfin"
    docker_restart_whitelist: str = ""

    # --- Диски ---
    # Формат: "метка:/путь,метка2:/путь2". Если метка не нужна — просто путь.
    # В docker-compose корень хоста примонтирован в /host/root.
    disk_paths: str = "root:/host/root"
    disk_alert_threshold: int = 90  # процент заполнения для предупреждения

    # --- Immich ---
    immich_url: str = ""
    immich_api_key: str = ""

    # --- Jellyfin ---
    jellyfin_url: str = ""
    jellyfin_api_key: str = ""
    jellyfin_user_id: str = ""

    # --- Transmission ---
    transmission_url: str = ""
    transmission_user: str = ""
    transmission_password: str = ""
    # Категории для выбора папки при добавлении торрента.
    # Формат: "Метка:/путь/внутри/контейнера/transmission" через запятую.
    # Пример: 🎬 Фильм:/downloads/movie,📺 Сериал:/downloads/show
    torrent_dirs: str = ""

    # --- Zapret (обход DPI на хосте) ---
    # Бот управляет systemd-сервисом zapret по SSH с forced command:
    # ключ позволяет выполнить только start/stop/restart/status.
    # Пустой zapret_ssh_user выключает функцию целиком.
    zapret_ssh_host: str = "host.docker.internal"
    zapret_ssh_port: int = 22
    zapret_ssh_user: str = ""
    zapret_ssh_key_path: str = "/app/ssh/id_ed25519"

    # --- Расписание ---
    daily_memory_time: str = "09:00"   # "HH:MM", пустая строка отключает
    weekly_report_day: str = "sun"      # mon/tue/wed/thu/fri/sat/sun
    weekly_report_time: str = "18:00"  # "HH:MM", пустая строка отключает
    monitor_interval_minutes: int = 15  # 0 отключает фоновый мониторинг
    # Проверка питания (сеть/аккумулятор) — для ноутбука-сервера это детектор
    # отключения света. Проверяем часто, алерт только при смене состояния.
    power_check_interval_seconds: int = 60  # 0 отключает
    battery_low_threshold: int = 25  # % заряда для критического алерта
    # Проверка завершённых закачек Transmission, секунды (0 отключает)
    torrent_check_interval_seconds: int = 120
    # Порог температуры CPU для алерта о перегреве, °C
    temp_alert_threshold: int = 85
    # Проверка доступности интернета для отчёта «интернет падал», сек (0 отключает)
    internet_check_interval_seconds: int = 60

    # --- Здоровье диска (SMART) ---
    # Диски через запятую, как их видит хост: /dev/nvme0n1 или /dev/sda.
    # Требует проброса устройств в docker-compose.yml (см. README). Пусто = выкл.
    smart_devices: str = ""

    # --- Лимит заряда батареи ---
    # Ноутбук 24/7 на зарядке убивает батарею; лимит 80% сильно продлевает ей жизнь.
    # Требует поддержки charge_control_end_threshold ноутбуком. 0 = не трогать.
    battery_charge_limit: int = 0

    # --- Вотчер веб-сервисов ---
    # "Метка:URL" через запятую, например "Navidrome:http://192.168.1.10:4533".
    # Бот следит за доступностью и алертит; статус виден в /today.
    watch_urls: str = ""

    # --- Мониторинг AWG-туннеля до VPS ---
    # TCP-проба адреса, достижимого ТОЛЬКО через туннель (обычно внутренний
    # IP VPS в VPN-сети и его SSH-порт). Пусто = выкл.
    awg_check_host: str = ""
    awg_check_port: int = 22

    # --- История метрик и /graph ---
    metrics_interval_minutes: int = 5   # период записи CPU/RAM/°C (0 = выкл)
    metrics_retention_days: int = 14    # сколько дней хранить
    metrics_db_path: str = "data/metrics.db"

    @cached_property
    def allowed_ids(self) -> set[int]:
        return {int(x.strip()) for x in self.allowed_user_ids.split(",") if x.strip()}

    @cached_property
    def restart_whitelist(self) -> list[str]:
        return [x.strip() for x in self.docker_restart_whitelist.split(",") if x.strip()]

    @cached_property
    def disks(self) -> list[tuple[str, str]]:
        """Список (метка, путь) для контроля дисков."""
        result: list[tuple[str, str]] = []
        for item in self.disk_paths.split(","):
            item = item.strip()
            if not item:
                continue
            if ":" in item and not item.startswith("/"):
                label, path = item.split(":", 1)
                result.append((label.strip(), path.strip()))
            else:
                result.append((item, item))
        return result

    @cached_property
    def torrent_categories(self) -> list[tuple[str, str]]:
        """Список (метка, путь в контейнере transmission) для выбора папки закачки."""
        result: list[tuple[str, str]] = []
        for item in self.torrent_dirs.split(","):
            item = item.strip()
            if not item or ":" not in item:
                continue
            label, path = item.split(":", 1)
            if label.strip() and path.strip().startswith("/"):
                result.append((label.strip(), path.strip()))
        return result

    @cached_property
    def smart_device_list(self) -> list[str]:
        return [x.strip() for x in self.smart_devices.split(",") if x.strip()]

    @cached_property
    def watch_services(self) -> list[tuple[str, str]]:
        """Список (метка, URL) для вотчера веб-сервисов."""
        result: list[tuple[str, str]] = []
        for item in self.watch_urls.split(","):
            item = item.strip()
            if not item or ":" not in item:
                continue
            label, url = item.split(":", 1)
            if label.strip() and url.strip().startswith(("http://", "https://")):
                result.append((label.strip(), url.strip()))
        return result

    @property
    def zapret_enabled(self) -> bool:
        return bool(self.zapret_ssh_user)

    @cached_property
    def notify_chat_id(self) -> int | None:
        if self.admin_chat_id:
            return self.admin_chat_id
        ids = sorted(self.allowed_ids)
        return ids[0] if ids else None


settings = Settings()
