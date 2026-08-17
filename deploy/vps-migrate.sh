#!/bin/bash
# Переезд VPN на новый VPS одной командой.
#
#   ./vps-migrate.sh root@новый-адрес
#
# Разворачивает чистый Ubuntu/Debian до готового сервера mieru и
# переключает на него домашнюю сторону: бота, зеркало подписок и панель.
#
# Главное свойство: ссылки у людей НЕ меняются. Они ведут на постоянный
# домен (publicUrl), а не на адрес VPS, поэтому обходить пятнадцать
# человек после переезда не нужно — клиенты подтянут новый адрес сами
# при очередном обновлении подписки.
#
# Состояние VPN — это два файла: mieru-server.json (логины и пароли) и
# mieru-meta.json (токены подписок). Пока они целы, новый сервер
# поднимается с теми же учётными данными.
#
# Запускать на домашнем сервере: здесь лежат ключи и конфиги.

set -euo pipefail

MIERU_VERSION="${MIERU_VERSION:-3.35.0}"
BOT_DIR=/home/san/Tg_bot
PANEL_DIR=/home/san/vpn-panel
MIRROR_UNIT=/etc/systemd/system/vpn-mirror.service
BACKUP_DIR=/home/san/vpn-backup
KEY_PUB="$BOT_DIR/ssh/id_ed25519_vpn.pub"
KEY_PRIV="$BOT_DIR/ssh/id_ed25519_vpn"

# Мультиплексирование ssh: пароль к новому серверу спросят один раз,
# дальше все команды идут по уже открытому соединению.
CTL=/tmp/vps-migrate-%r@%h:%p
SSH_OPTS=(-o ControlMaster=auto -o "ControlPath=$CTL" -o ControlPersist=15m
          -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20)

NEW="${1:-}"
[ -n "$NEW" ] || { echo "укажите адрес: $0 root@новый-ip"; exit 1; }
NEW_HOST="${NEW#*@}"
[[ "$NEW" == *@* ]] || NEW="root@$NEW"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   ✓ %s\n' "$*"; }
die()  { printf '\n\033[31mОСТАНОВ: %s\033[0m\n' "$*" >&2; exit 1; }
rsh()  { ssh "${SSH_OPTS[@]}" "$NEW" "$@"; }

# --- откуда берём состояние --------------------------------------------
# С живого старого сервера — свежее всего. Если он уже недоступен (ровно
# тот случай, ради которого переезд и затевается), берём последний бэкап.
OLD_HOST=$(grep -oP '^VPN_SSH_HOST=\K.*' "$BOT_DIR/.env" 2>/dev/null || true)
STATE_SRC=""
if [ -n "$OLD_HOST" ] && ssh -i "$KEY_PRIV" -n -o BatchMode=yes -o ConnectTimeout=10 \
     "root@$OLD_HOST" status >/dev/null 2>&1; then
    STATE_SRC="old-server"
else
    LAST_BACKUP=$(ls -d "$BACKUP_DIR"/*/ 2>/dev/null | tail -1 || true)
    [ -n "$LAST_BACKUP" ] || die "старый VPS недоступен и бэкапа состояния нет в $BACKUP_DIR"
    STATE_SRC="$LAST_BACKUP"
fi

say "Переезд VPN на $NEW_HOST"
echo "   старый сервер: ${OLD_HOST:-неизвестен}"
echo "   состояние из:  $STATE_SRC"
echo "   версия mieru:  $MIERU_VERSION"

# --- 1. проверки -------------------------------------------------------
say "1/9  Проверяю новый сервер"
[ -f "$KEY_PUB" ] || die "нет публичного ключа бота: $KEY_PUB"
rsh true 2>/dev/null || die "не подключиться к $NEW (проверьте адрес, пароль или ключ)"
rsh "[ \$(id -u) -eq 0 ]" || die "нужен root на новом сервере"
OSNAME=$(rsh ". /etc/os-release && echo \$ID \$VERSION_ID")
ARCH=$(rsh "dpkg --print-architecture")
[ "$ARCH" = amd64 ] || die "ожидался amd64, а там $ARCH"
ok "$OSNAME, $ARCH, root есть"

# --- 2. система --------------------------------------------------------
say "2/9  Базовая настройка"
rsh "export DEBIAN_FRONTEND=noninteractive
     apt-get update -qq
     apt-get install -y -qq curl nginx >/dev/null" || die "не поставить curl/nginx"
# BBR: на маршруте до России кратно поднимает одиночный поток. Без него
# окно перегрузки схлопывается и получается около мегабита на соединение.
rsh "printf 'net.core.default_qdisc = fq\nnet.ipv4.tcp_congestion_control = bbr\n' \
       > /etc/sysctl.d/99-bbr.conf
     sysctl -p /etc/sysctl.d/99-bbr.conf >/dev/null"
CC=$(rsh "sysctl -n net.ipv4.tcp_congestion_control")
[ "$CC" = bbr ] || die "BBR не включился (сейчас $CC)"
ok "пакеты, BBR включён"

# --- 3. mieru ----------------------------------------------------------
say "3/9  Ставлю mieru $MIERU_VERSION"
if rsh "command -v mita >/dev/null"; then
    ok "уже установлен ($(rsh 'mita version 2>/dev/null | head -1'))"
else
    URL="https://github.com/enfein/mieru/releases/download/v${MIERU_VERSION}/mita_${MIERU_VERSION}_${ARCH}.deb"
    rsh "curl -fsSL -o /tmp/mita.deb '$URL' && dpkg -i /tmp/mita.deb >/dev/null 2>&1 && rm -f /tmp/mita.deb" \
        || die "не установить mita с $URL"
    ok "установлен"
fi

# --- 4. скрипты управления ---------------------------------------------
say "4/9  Кладу скрипты управления"
for f in mieru-user vpn-bot-ctl; do
    src="$(dirname "$0")/${f}.py"
    [ -f "$src" ] || die "нет $src"
    # sed режет возможные CRLF: файл, отредактированный на Windows, ломает
    # shebang и скрипт не запускается вовсе.
    sed 's/\r$//' "$src" | rsh "cat > /usr/local/bin/$f && chmod 755 /usr/local/bin/$f"
done
ok "mieru-user и vpn-bot-ctl на месте"

# --- 5. состояние ------------------------------------------------------
say "5/9  Переношу пользователей и токены"
if [ "$STATE_SRC" = old-server ]; then
    for f in mieru-server.json mieru-meta.json; do
        ssh -i "$KEY_PRIV" -n -o BatchMode=yes "root@$OLD_HOST" "cat /root/$f" \
            | rsh "cat > /root/$f && chmod 600 /root/$f"
    done
else
    for f in mieru-server.json mieru-meta.json; do
        [ -f "$STATE_SRC/$f" ] || die "в бэкапе нет $f"
        rsh "cat > /root/$f && chmod 600 /root/$f" < "$STATE_SRC/$f"
    done
fi
USERS=$(rsh "python3 -c \"import json;print(len(json.load(open('/root/mieru-server.json'))['users']))\"")
ok "перенесено пользователей: $USERS"

# --- 6. запуск mieru ---------------------------------------------------
say "6/9  Поднимаю mieru"
# mita берёт конфиг из того же файла, что и mieru-user, — просто скармливаем.
rsh "mita apply config /root/mieru-server.json >/dev/null 2>&1 || true
     systemctl enable mita >/dev/null 2>&1 || true
     mita start >/dev/null 2>&1 || systemctl start mita >/dev/null 2>&1 || true"
sleep 3
rsh "mita status 2>/dev/null | grep -q RUNNING" || die "mita не запустился — смотрите 'mita status' на сервере"
PORTS=$(rsh "ss -tln 2>/dev/null | grep -cE ':300[0-9][0-9]'")
ok "работает, портов слушает: $PORTS"

# --- 7. раздача подписок ----------------------------------------------
say "7/9  Настраиваю раздачу подписок"
# Зеркало на домашнем сервере забирает конфиги отсюда напрямую, поэтому
# порт 8080 обязан отвечать, даже когда людям раздаются ссылки на домен.
rsh "mkdir -p /opt/mieru-sub
     cat > /etc/nginx/sites-available/mieru-sub <<'CONF'
server {
    listen 8080 default_server;
    root /opt/mieru-sub;
    autoindex off;
    location / { default_type text/plain; try_files \$uri =404; }
}
CONF
     ln -sf /etc/nginx/sites-available/mieru-sub /etc/nginx/sites-enabled/mieru-sub
     rm -f /etc/nginx/sites-enabled/default
     nginx -t >/dev/null 2>&1 && systemctl reload nginx"
ok "nginx отдаёт /opt/mieru-sub на 8080"

# --- 8. ключ бота ------------------------------------------------------
say "8/9  Привязываю ключ бота к обёртке"
PUB=$(cat "$KEY_PUB")
# Именно forced command делает утёкший ключ безопасным: он может выполнить
# только vpn-bot-ctl, который принимает семь действий и проверяет аргументы.
rsh "mkdir -p /root/.ssh && chmod 700 /root/.ssh
     touch /root/.ssh/authorized_keys
     grep -q '$(echo "$PUB" | awk '{print $2}')' /root/.ssh/authorized_keys \
       || echo 'command=\"/usr/local/bin/vpn-bot-ctl\",no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding $PUB' >> /root/.ssh/authorized_keys
     chmod 600 /root/.ssh/authorized_keys"
ssh -i "$KEY_PRIV" -n -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
    "root@$NEW_HOST" status >/dev/null 2>&1 || die "бот не смог достучаться через forced command"
ok "проверено: бот ходит на новый сервер"

# --- 9. переключение домашней стороны ----------------------------------
say "9/9  Переключаю дом на новый адрес"
rsh "mieru-user server '$NEW_HOST' >/dev/null" || die "не переписать подписки"
ok "подписки перегенерированы под $NEW_HOST"

sed -i "s|^VPN_SSH_HOST=.*|VPN_SSH_HOST=$NEW_HOST|" "$BOT_DIR/.env"
sed -i "s|^VPN_PANEL_HOST=.*|VPN_PANEL_HOST=$NEW_HOST|" "$PANEL_DIR/.env"
sudo sed -i "s|VPN_MIRROR_HOST=[^ ]*|VPN_MIRROR_HOST=$NEW_HOST|" "$MIRROR_UNIT" 2>/dev/null || true
grep -q 'VPN_MIRROR_HOST' "$MIRROR_UNIT" 2>/dev/null \
  || sudo sed -i "/^\[Service\]/a Environment=VPN_MIRROR_HOST=$NEW_HOST" "$MIRROR_UNIT"

# known_hosts панели: без записи ssh из контейнера откажется соединяться.
ssh-keyscan "$NEW_HOST" 2>/dev/null | grep -v '^#' > "$PANEL_DIR/ssh_known_hosts"

sudo systemctl daemon-reload
(cd "$BOT_DIR" && sudo docker compose up -d >/dev/null 2>&1)
(cd "$PANEL_DIR" && sudo docker compose up -d >/dev/null 2>&1)
sudo systemctl start vpn-mirror.service
ok "бот, панель и зеркало перезапущены"

# --- проверки ----------------------------------------------------------
say "Проверяю результат"
sleep 5
SUBS=$(ls /home/san/vpn-mirror/www/sub/ 2>/dev/null | wc -l)
[ "$SUBS" -ge 1 ] || die "зеркало не забрало подписки с нового сервера"
ok "подписок в зеркале: $SUBS"

FIRST=$(ls /home/san/vpn-mirror/www/sub/ | head -1)
grep -q "server: $NEW_HOST" "/home/san/vpn-mirror/www/sub/$FIRST" \
  && ok "в конфигах прописан новый адрес" \
  || die "в подписке остался старый адрес"

# Свежий бэкап состояния уже с нового сервера — на случай следующего переезда.
STAMP=$(date +%Y%m%d)
mkdir -p "$BACKUP_DIR/$STAMP"
for f in mieru-server.json mieru-meta.json; do
    rsh "cat /root/$f" > "$BACKUP_DIR/$STAMP/$f"
done
chmod -R go-rwx "$BACKUP_DIR"
ok "состояние сохранено в $BACKUP_DIR/$STAMP"

ssh -O exit "${SSH_OPTS[@]}" "$NEW" 2>/dev/null || true

cat <<FINAL

Готово. VPN работает на $NEW_HOST.

Что важно: ссылки у людей не изменились — они ведут на домен, а не на
адрес сервера. Клиенты подтянут новый адрес сами при очередном
обновлении подписки, обходить никого не нужно.

Старый сервер можно гасить не раньше, чем через сутки: у тех, кто
давно не открывал клиент, подписка ещё не обновилась.
FINAL
