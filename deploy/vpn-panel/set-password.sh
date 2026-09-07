#!/bin/bash
# Смена логина и пароля панели.
#
# Пароль нигде не хранится: в .env кладём только sha256-хеш, поэтому
# «посмотреть текущий пароль» невозможно — забыли, значит задаём новый.
set -e
cd "$(dirname "$0")"

[ -f .env ] || { echo "нет .env рядом со скриптом"; exit 1; }

read -rp "Логин [оставить как есть — Enter]: " LOGIN
read -rsp "Новый пароль: " PASS; echo
read -rsp "Ещё раз: " PASS2; echo

[ "$PASS" = "$PASS2" ] || { echo "пароли не совпали"; exit 1; }
[ ${#PASS} -ge 12 ] || { echo "пароль короче 12 символов — панель смотрит в интернет, так нельзя"; exit 1; }

HASH=$(python3 -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" "$PASS")

cp .env .env.bak
sed -i "s|^VPN_PANEL_PASSWORD_HASH=.*|VPN_PANEL_PASSWORD_HASH=$HASH|" .env
if [ -n "$LOGIN" ]; then
  if grep -q '^VPN_PANEL_LOGIN=' .env; then
    sed -i "s|^VPN_PANEL_LOGIN=.*|VPN_PANEL_LOGIN=$LOGIN|" .env
  else
    echo "VPN_PANEL_LOGIN=$LOGIN" >> .env
  fi
fi

# Сессии подписаны отдельным секретом и переживают перезапуск, так что
# старый вход останется живым. Меняем и секрет — тогда чужая сессия,
# если она была, тоже разлогинится.
sed -i "s|^VPN_PANEL_SECRET=.*|VPN_PANEL_SECRET=$(python3 -c 'import secrets;print(secrets.token_hex(32))')|" .env

sudo docker compose up -d >/dev/null 2>&1
echo "готово: пароль обновлён, все сессии сброшены"
