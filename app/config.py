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

    # --- Расписание ---
    daily_memory_time: str = "09:00"   # "HH:MM", пустая строка отключает
    weekly_report_day: str = "sun"      # mon/tue/wed/thu/fri/sat/sun
    weekly_report_time: str = "18:00"  # "HH:MM", пустая строка отключает
    monitor_interval_minutes: int = 15  # 0 отключает фоновый мониторинг
    # Проверка питания (сеть/аккумулятор) — для ноутбука-сервера это детектор
    # отключения света. Проверяем часто, алерт только при смене состояния.
    power_check_interval_seconds: int = 60  # 0 отключает
    battery_low_threshold: int = 25  # % заряда для критического алерта

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
    def notify_chat_id(self) -> int | None:
        if self.admin_chat_id:
            return self.admin_chat_id
        ids = sorted(self.allowed_ids)
        return ids[0] if ids else None


settings = Settings()
