#!/bin/sh
# Корректное выключение сервера при разряде батареи.
# Ноутбук = UPS, но батарея маленькая (32 Вт·ч, ~1.5-2 ч работы).
# Раньше сервер умирал на 0% -> грязная ФС, потеря IP-адреса, риск данных.
# Теперь: предупреждение, затем штатное выключение с запасом заряда.
BAT=/sys/class/power_supply/BAT1
AC=/sys/class/power_supply/AC
WARN=20          # % — предупредить
SHUTDOWN=12      # % — выключаться
STATE=/run/battery-guard.warned

[ -d "$BAT" ] || exit 0
CAP=$(cat "$BAT/capacity" 2>/dev/null) || exit 0
ST=$(cat "$BAT/status" 2>/dev/null)

notify() {
  ENV=/home/san/Tg_bot/.env
  TOKEN=$(grep '^BOT_TOKEN=' "$ENV" 2>/dev/null | cut -d= -f2-)
  CHAT=$(grep '^ALLOWED_USER_IDS=' "$ENV" 2>/dev/null | cut -d= -f2- | cut -d, -f1)
  [ -n "$TOKEN" ] && [ -n "$CHAT" ] && curl -s --max-time 10 \
    --socks5-hostname 127.0.0.1:1080 \
    -d "chat_id=$CHAT" --data-urlencode "text=$1" \
    "https://api.telegram.org/bot${TOKEN}/sendMessage" >/dev/null 2>&1
}

# На зарядке — сбрасываем состояние и выходим
if [ "$ST" != "Discharging" ]; then
  rm -f "$STATE"
  exit 0
fi

if [ "$CAP" -le "$SHUTDOWN" ]; then
  logger -t battery-guard "Заряд ${CAP}% — штатное выключение сервера"
  notify "🔌 Заряд батареи ${CAP}%. Сервер корректно выключается, чтобы не потерять данные. После появления света включите ноутбук кнопкой."
  sleep 5
  /sbin/shutdown -h now "Критический заряд батареи"
elif [ "$CAP" -le "$WARN" ] && [ ! -f "$STATE" ]; then
  logger -t battery-guard "Заряд ${CAP}% — предупреждение"
  notify "🪫 Заряд батареи ${CAP}%, света всё нет. При ${SHUTDOWN}% сервер выключится сам (штатно)."
  touch "$STATE"
fi
exit 0
