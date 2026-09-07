#!/bin/sh
# Защита статического IP сервера от захвата DHCP-клиентами.
# Роутер "тупой" (нет резерваций DHCP), поэтому после отключения света
# он может выдать 192.168.101.7 чужому устройству. Здесь мы:
#  1) регулярно объявляем сети свой MAC для этого адреса (gratuitous ARP)
#  2) детектим конфликт и шлём алерт в Telegram
IFACE=enp3s0
IP=192.168.101.7
STATE=/run/ip-defend.conflict

command -v arping >/dev/null 2>&1 || exit 0
ip -br addr show "$IFACE" 2>/dev/null | grep -q "$IP" || exit 0

# 1. Громко объявляем: этот IP — наш
arping -U -c 2 -I "$IFACE" "$IP" >/dev/null 2>&1

# 2. Проверяем, не отвечает ли кто-то ещё на наш адрес
FOREIGN=$(arping -D -c 3 -I "$IFACE" "$IP" 2>/dev/null | grep -oE '\[([0-9A-Fa-f:]{17})\]' | tr -d '[]' | head -1)

if [ -n "$FOREIGN" ]; then
  logger -t ip-defend "КОНФЛИКТ IP: $IP также занят устройством $FOREIGN"
  # алерт в Telegram (не чаще раза в час на один и тот же MAC)
  PREV=$(cat "$STATE" 2>/dev/null)
  NOW=$(date +%s)
  LAST=$(echo "$PREV" | cut -d' ' -f2)
  [ -z "$LAST" ] && LAST=0
  if [ "$PREV" != "${FOREIGN} ${LAST}" ] || [ $((NOW - LAST)) -gt 3600 ]; then
    ENV=/home/san/Tg_bot/.env
    TOKEN=$(grep '^BOT_TOKEN=' "$ENV" 2>/dev/null | cut -d= -f2-)
    CHAT=$(grep '^ALLOWED_USER_IDS=' "$ENV" 2>/dev/null | cut -d= -f2- | cut -d, -f1)
    if [ -n "$TOKEN" ] && [ -n "$CHAT" ]; then
      MSG="⚠️ Конфликт IP-адреса! Устройство $FOREIGN заняло адрес сервера $IP. Интернет через шлюз может работать нестабильно. Сервер отбивает адрес автоматически; если не поможет — отключите это устройство от сети."
      curl -s --max-time 10 --socks5-hostname 127.0.0.1:1080 \
        -d "chat_id=$CHAT" --data-urlencode "text=$MSG" \
        "https://api.telegram.org/bot${TOKEN}/sendMessage" >/dev/null 2>&1
    fi
    echo "$FOREIGN $NOW" > "$STATE"
  fi
  # 3. Отбиваем адрес: серия настойчивых анонсов
  arping -U -c 5 -I "$IFACE" "$IP" >/dev/null 2>&1
else
  rm -f "$STATE"
fi
exit 0
