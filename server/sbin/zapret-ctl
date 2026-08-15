#!/usr/bin/env bash
# Forced command для SSH-ключа бота HomePilot.
# Прописывается в ~/.ssh/authorized_keys хоста как command="...".
# Бот НЕ получает shell: что бы он ни отправил, сюда приходит только имя
# действия в $SSH_ORIGINAL_COMMAND, а мы разрешаем строго белый список.
#
# Установка на хосте (Ubuntu-шлюз):
#   sudo install -m 755 deploy/zapret-ctl.sh /usr/local/bin/zapret-ctl
#   и разрешить пользователю бота управлять сервисом без пароля — см. README.

set -euo pipefail

action="${SSH_ORIGINAL_COMMAND:-status}"

case "$action" in
  start|stop|restart)
    exec sudo -n systemctl "$action" zapret
    ;;
  status)
    # без пейджера и цвета, чтобы аккуратно влезло в Telegram
    exec systemctl --no-pager --plain status zapret
    ;;
  is-active)
    # systemctl возвращает код !=0 для inactive — не даём set -e уронить скрипт
    exec systemctl is-active zapret || true
    ;;
  *)
    echo "Запрещено. Разрешены: start, stop, restart, status, is-active" >&2
    exit 1
    ;;
esac
