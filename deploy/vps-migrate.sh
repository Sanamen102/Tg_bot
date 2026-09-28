#!/bin/bash
# Переезд VPN на новый VPS одной командой.
#
#   ./vps-migrate.sh root@новый-адрес            переезд
#   ./vps-migrate.sh --force root@новый-адрес    даже если check-host не видит
#                                                новый адрес из России
#   ./vps-migrate.sh root@старый-адрес           откат: то же самое в обратную
#                                                сторону, пока старый жив
#
# Запускать на домашнем сервере от san (не через sudo): здесь лежат ключи и
# конфиги. Пароль root нового сервера спросят один раз.
#
# Что происходит:
#   1-8  новый сервер доводится до готового mieru с теми же пользователями,
#        паролями и токенами подписок. Дом при этом ещё не тронут.
#   9    проверка: видно ли новый адрес из России (check-host) и открывается
#        ли через него Telegram на самом деле. Не прошло — остановка, всё
#        продолжает работать на старом сервере.
#   10   переключение дома: бот, панель, зеркало подписок, мосты бота и
#        телевизоров. Мосты обновляются сразу, не дожидаясь своего часа.
#   11   старый сервер начинает раздавать подписки с НОВЫМ адресом — так
#        переезжают люди, у которых ещё старые ссылки вида http://IP:8080/…
#
# Повторный запуск безопасен: каждый шаг либо уже сделан, либо доделывается.

set -euo pipefail

MIERU_VERSION="${MIERU_VERSION:-3.35.0}"
HERE=$(cd "$(dirname "$0")" && pwd)
BOT_DIR=/home/san/Tg_bot
PANEL_DIR=/home/san/vpn-panel
MIRROR_UNIT=/etc/systemd/system/vpn-mirror.service
MIRROR_WWW=/home/san/vpn-mirror/www/sub
BACKUP_DIR=/home/san/vpn-backup
KEY_PUB="$BOT_DIR/ssh/id_ed25519_vpn.pub"
KEY_PRIV="$BOT_DIR/ssh/id_ed25519_vpn"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   ✓ %s\n' "$*"; }
warn() { printf '   \033[33m! %s\033[0m\n' "$*"; }
die()  { printf '\n\033[31mОСТАНОВ: %s\033[0m\n' "$*" >&2; exit 1; }

FORCE=0
NEW=""
for a in "$@"; do
    case "$a" in
        --force) FORCE=1 ;;
        -*) die "неизвестный флаг $a" ;;
        *) NEW="$a" ;;
    esac
done
[ -n "$NEW" ] || die "укажите адрес: $0 root@новый-ip"
[[ "$NEW" == *@* ]] || NEW="root@$NEW"
NEW_HOST="${NEW#*@}"
[ "$(id -u)" -ne 0 ] || die "запускайте от san, без sudo: ключи и known_hosts — его"

# Мультиплексирование ssh: пароль к новому серверу спросят один раз,
# дальше все команды идут по уже открытому соединению.
CTL=/tmp/vps-migrate-%r@%h:%p
SSH_OPTS=(-o ControlMaster=auto -o "ControlPath=$CTL" -o ControlPersist=15m
          -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20)
rsh() { ssh "${SSH_OPTS[@]}" "$NEW" "$@"; }

# Ключ бота на VPS прибит к forced command: выполняет только действия
# vpn-bot-ctl (status/state/server/…), любой другой командой он не пустит.
ctl() {
    local host="$1"; shift
    ssh -i "$KEY_PRIV" -n -o BatchMode=yes -o ConnectTimeout=10 \
        -o StrictHostKeyChecking=accept-new "root@$host" "$@"
}

TMP=$(mktemp -d)
cleanup() { rm -rf "$TMP"; ssh -O exit "${SSH_OPTS[@]}" "$NEW" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# --- откуда берём состояние --------------------------------------------
OLD_HOST=$(grep -oP '^VPN_SSH_HOST=\K.*' "$BOT_DIR/.env" 2>/dev/null || true)
PUBLIC=$(grep -oP 'VPN_MIRROR_PUBLIC_URL=\K\S+' "$MIRROR_UNIT" 2>/dev/null || true)
[ -n "$PUBLIC" ] || die "в $MIRROR_UNIT нет VPN_MIRROR_PUBLIC_URL — не знаю постоянный адрес подписок"
SUB_BASE="${PUBLIC%/}/sub"

# С живого текущего сервера — свежее всего. Если он уже недоступен (ровно
# тот случай, ради которого переезд и затевается), берём последний бэкап.
OLD_ALIVE=0
if [ -n "$OLD_HOST" ] && ctl "$OLD_HOST" status >/dev/null 2>&1; then
    OLD_ALIVE=1
    STATE_SRC="сервер $OLD_HOST"
else
    LAST_BACKUP=$(ls -d "$BACKUP_DIR"/*/ 2>/dev/null | tail -1 || true)
    [ -n "$LAST_BACKUP" ] || die "старый VPS недоступен и бэкапа состояния нет в $BACKUP_DIR"
    STATE_SRC="бэкап ${LAST_BACKUP%/}"
fi

say "Переезд VPN на $NEW_HOST"
echo "   сейчас работает:  ${OLD_HOST:-неизвестно}$([ $OLD_ALIVE = 1 ] && echo ' (жив)' || echo ' (НЕ отвечает)')"
echo "   состояние из:     $STATE_SRC"
echo "   ссылки у людей:   $SUB_BASE/…"
[ "$NEW_HOST" != "$OLD_HOST" ] || warn "это и есть текущий сервер — повторный прогон, доделаю то, что не доделано"

# --- 1. проверки -------------------------------------------------------
say "1/11  Проверяю новый сервер"
[ -f "$KEY_PUB" ] || die "нет публичного ключа бота: $KEY_PUB"
for f in mieru-user.py vpn-bot-ctl.py vpn-bridges.py ru-reach.py; do
    [ -f "$HERE/$f" ] || die "рядом со скриптом нет $f"
done
rsh true || die "не подключиться к $NEW (проверьте адрес и пароль)"
rsh "[ \$(id -u) -eq 0 ]" || die "нужен root на новом сервере"
OSNAME=$(rsh ". /etc/os-release && echo \$ID \$VERSION_ID")
ARCH=$(rsh "dpkg --print-architecture")
[ "$ARCH" = amd64 ] || die "ожидался amd64, а там $ARCH"
ok "$OSNAME, $ARCH, root есть"

# --- 2. система --------------------------------------------------------
say "2/11  Базовая настройка"
rsh "export DEBIAN_FRONTEND=noninteractive
     apt-get update -qq
     apt-get install -y -qq curl nginx python3 >/dev/null" || die "не поставить curl/nginx/python3"
# BBR: на маршруте до России кратно поднимает одиночный поток. Без него
# окно перегрузки схлопывается и получается около мегабита на соединение.
rsh "printf 'net.core.default_qdisc = fq\nnet.ipv4.tcp_congestion_control = bbr\n' \
       > /etc/sysctl.d/99-bbr.conf
     sysctl -p /etc/sysctl.d/99-bbr.conf >/dev/null"
CC=$(rsh "sysctl -n net.ipv4.tcp_congestion_control")
[ "$CC" = bbr ] || die "BBR не включился (сейчас $CC)"
ok "пакеты, BBR включён"

# --- 3. mieru ----------------------------------------------------------
say "3/11  Ставлю mieru $MIERU_VERSION"
if rsh "command -v mita >/dev/null"; then
    ok "уже установлен ($(rsh 'mita version 2>/dev/null | head -1'))"
else
    URL="https://github.com/enfein/mieru/releases/download/v${MIERU_VERSION}/mita_${MIERU_VERSION}_${ARCH}.deb"
    rsh "curl -fsSL -o /tmp/mita.deb '$URL' && dpkg -i /tmp/mita.deb >/dev/null 2>&1 && rm -f /tmp/mita.deb" \
        || die "не установить mita с $URL"
    ok "установлен"
fi

# --- 4. скрипты управления ---------------------------------------------
say "4/11  Кладу скрипты управления"
for f in mieru-user vpn-bot-ctl; do
    # sed режет возможные CRLF: файл, отредактированный на Windows, ломает
    # shebang и скрипт не запускается вовсе.
    sed 's/\r$//' "$HERE/${f}.py" | rsh "cat > /usr/local/bin/$f && chmod 755 /usr/local/bin/$f"
done
ok "mieru-user и vpn-bot-ctl на месте"

# --- 5. состояние ------------------------------------------------------
say "5/11  Переношу пользователей и токены"
if [ $OLD_ALIVE = 1 ]; then
    ctl "$OLD_HOST" state > "$TMP/state.json" || die "сервер $OLD_HOST не отдал состояние"
    python3 - "$TMP" <<'PY' || die "состояние со старого сервера негодное — ничего не переношу"
import json, sys, pathlib
tmp = pathlib.Path(sys.argv[1])
d = json.loads((tmp / "state.json").read_text())
if not d.get("ok"):
    raise SystemExit("сервер вернул ошибку: %s" % d.get("error"))
if not d["server"].get("users") or not d["meta"].get("tokens"):
    raise SystemExit("в ответе нет пользователей или токенов")
(tmp / "mieru-server.json").write_text(json.dumps(d["server"], indent=2, ensure_ascii=False))
(tmp / "mieru-meta.json").write_text(json.dumps(d["meta"], indent=2, ensure_ascii=False))
PY
    SRC_DIR="$TMP"
else
    SRC_DIR="${LAST_BACKUP%/}"
    warn "старый сервер не отвечает — беру бэкап от $(basename "$SRC_DIR")."
    warn "Кого заводили позже этой даты, придётся завести заново."
fi
for f in mieru-server.json mieru-meta.json; do
    [ -s "$SRC_DIR/$f" ] || die "нет $f в $SRC_DIR"
    rsh "umask 077 && cat > /root/$f.tmp && mv /root/$f.tmp /root/$f" < "$SRC_DIR/$f"
done
read -r USERS LO HI < <(python3 - "$SRC_DIR/mieru-server.json" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
pb = next(p for p in c["portBindings"] if p.get("protocol") == "TCP")
lo, _, hi = (pb.get("portRange") or str(pb["port"])).partition("-")
print(len(c["users"]), lo, hi or lo)
PY
) || true
[ -n "${HI:-}" ] || die "не разобрать mieru-server.json (пользователи/порты)"
ok "перенесено пользователей: $USERS, порты $LO-$HI"

# --- 6. файрвол и запуск mieru -----------------------------------------
say "6/11  Открываю порты и поднимаю mieru"
if rsh "command -v ufw >/dev/null && ufw status | grep -q 'Status: active'"; then
    rsh "ufw allow 22/tcp >/dev/null; ufw allow 8080/tcp >/dev/null
         ufw allow $LO:$HI/tcp >/dev/null; ufw allow $LO:$HI/udp >/dev/null"
    ok "ufw: открыты 22, 8080, $LO-$HI"
elif rsh "iptables -S INPUT 2>/dev/null | head -1 | grep -q DROP"; then
    warn "на сервере iptables с политикой DROP — если проверка ниже не пройдёт, дело в нём"
else
    ok "локального файрвола нет"
fi
# Файрвол в панели хостера отсюда не виден — его поймает проверка на шаге 9.
rsh "set -e
     systemctl enable --now mita >/dev/null 2>&1 || true
     sleep 2
     mita apply config /root/mieru-server.json >/dev/null
     if mita status 2>/dev/null | grep -q RUNNING; then mita reload >/dev/null; else mita start >/dev/null; fi" \
    || die "mita не принял конфиг — смотрите 'mita status' на сервере"
sleep 3
rsh "mita status 2>/dev/null | grep -q RUNNING" || die "mita не запустился — смотрите 'mita status' на сервере"
PORTS=$(rsh "ss -tln 2>/dev/null | awk '{print \$4}' | grep -oE '[0-9]+\$' | awk '\$1>=$LO && \$1<=$HI' | wc -l")
[ "$PORTS" -gt 0 ] || die "mita запущен, но ни одного порта $LO-$HI не слушает"
ok "работает, слушает портов: $PORTS"

# --- 7. раздача подписок ----------------------------------------------
say "7/11  Настраиваю раздачу подписок"
# Зеркало на домашнем сервере забирает подписки отсюда напрямую, поэтому
# порт 8080 обязан отвечать, даже когда людям раздаются ссылки на домен.
if rsh "curl -s -o /dev/null -m 5 -w '%{http_code}' http://127.0.0.1:8080/ | grep -qE '^(404|200)\$'"; then
    ok "на 8080 раздача уже работает — не трогаю"
else
    rsh "mkdir -p /opt/mieru-sub
         cat > /etc/nginx/sites-available/mieru-sub <<'CONF'
server {
    listen 8080 default_server;
    root /opt/mieru-sub;
    autoindex off;
    access_log /var/log/nginx/mieru-sub.log;
    location / {
        default_type text/plain;
        # Clash-клиенты сами обновляют подписку раз в сутки.
        add_header profile-update-interval 24;
        try_files \$uri =404;
    }
    location = / { return 404; }
}
CONF
         ln -sf /etc/nginx/sites-available/mieru-sub /etc/nginx/sites-enabled/mieru-sub
         rm -f /etc/nginx/sites-enabled/default
         nginx -t >/dev/null 2>&1 && systemctl enable nginx >/dev/null 2>&1 && systemctl restart nginx" \
        || die "nginx не принял конфиг раздачи"
    ok "nginx отдаёт /opt/mieru-sub на 8080"
fi
rsh "mieru-user server '$NEW_HOST' >/dev/null" || die "не сгенерировать подписки"
ok "подписки сгенерированы под $NEW_HOST"

# --- 8. ключ бота ------------------------------------------------------
say "8/11  Привязываю ключ бота к обёртке"
PUB=$(cat "$KEY_PUB")
# Именно forced command делает утёкший ключ безопасным: он может выполнить
# только vpn-bot-ctl, который принимает несколько действий и проверяет аргументы.
rsh "mkdir -p /root/.ssh && chmod 700 /root/.ssh
     touch /root/.ssh/authorized_keys
     grep -q '$(echo "$PUB" | awk '{print $2}')' /root/.ssh/authorized_keys \
       || echo 'command=\"/usr/local/bin/vpn-bot-ctl\",no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding $PUB' >> /root/.ssh/authorized_keys
     chmod 600 /root/.ssh/authorized_keys"
ctl "$NEW_HOST" status >/dev/null 2>&1 || die "бот не смог достучаться через forced command"
ok "проверено: бот ходит на новый сервер"

# --- 9. проверка перед переключением -----------------------------------
say "9/11  Проверяю новый сервер снаружи, дом пока не трогаю"
set +e
python3 "$HERE/ru-reach.py" "$NEW_HOST" "$LO" "$HI"
REACH=$?
set -e
case $REACH in
    0) ok "адрес виден со всех российских узлов check-host" ;;
    3) warn "виден не со всех узлов — у части операторов адрес может быть заблокирован" ;;
    1) warn "check-host не ответил — проверку из России пропускаю" ;;
    2) if [ $FORCE = 1 ]; then warn "из России не виден, но указан --force — продолжаю"
       else die "новый адрес не виден из России. Ничего не переключено, всё работает по-старому.
Проверьте файрвол в панели хостера (нужны $LO-$HI tcp+udp и 8080/tcp) или берите другой IP.
Если уверены в адресе — запустите с --force"; fi ;;
esac
sudo python3 "$HERE/vpn-bridges.py" --probe "$NEW_HOST" \
    || die "через новый сервер Telegram не открывается. Ничего не переключено, всё работает по-старому."

# --- 10. переключение домашней стороны ---------------------------------
say "10/11  Переключаю дом на новый адрес"
sed -i "s|^VPN_SSH_HOST=.*|VPN_SSH_HOST=$NEW_HOST|" "$BOT_DIR/.env"
if [ -f "$PANEL_DIR/.env" ]; then
    sed -i "s|^VPN_PANEL_HOST=.*|VPN_PANEL_HOST=$NEW_HOST|" "$PANEL_DIR/.env"
    grep -q '^VPN_PANEL_HOST=' "$PANEL_DIR/.env" || echo "VPN_PANEL_HOST=$NEW_HOST" >> "$PANEL_DIR/.env"
    # known_hosts панели: без записи ssh из контейнера откажется соединяться.
    ssh-keyscan -T 10 "$NEW_HOST" 2>/dev/null | grep -v '^#' > "$PANEL_DIR/ssh_known_hosts"
fi
if grep -q 'VPN_MIRROR_HOST=' "$MIRROR_UNIT"; then
    sudo sed -i "s|VPN_MIRROR_HOST=[^ ]*|VPN_MIRROR_HOST=$NEW_HOST|" "$MIRROR_UNIT"
else
    sudo sed -i "/^\[Service\]/a Environment=VPN_MIRROR_HOST=$NEW_HOST" "$MIRROR_UNIT"
fi
sudo systemctl daemon-reload
ok "адрес прописан в боте, панели и зеркале"

sudo systemctl start vpn-mirror.service || true
STALE=$(grep -L "server: $NEW_HOST" "$MIRROR_WWW"/* 2>/dev/null | wc -l)
TOTAL=$(ls "$MIRROR_WWW" 2>/dev/null | wc -l)
[ "$TOTAL" -ge 1 ] && [ "$STALE" -eq 0 ] \
    || die "зеркало не обновилось ($STALE из $TOTAL подписок со старым адресом) — смотрите journalctl -u vpn-mirror"
ok "зеркало: все $TOTAL подписок уже с новым адресом"

# Мосты бота и телевизоров: постоянный адрес подписки + свежий кэш сразу.
sudo python3 "$HERE/vpn-bridges.py" "$SUB_BASE" --refresh \
    || warn "с мостом что-то не так — см. выше; бот может быть без Telegram"

(cd "$BOT_DIR" && sudo docker compose up -d >/dev/null 2>&1) || warn "бот не перезапустился"
[ -f "$PANEL_DIR/docker-compose.yml" ] && { (cd "$PANEL_DIR" && sudo docker compose up -d >/dev/null 2>&1) || warn "панель не перезапустилась"; }
ok "бот и панель перезапущены"

# --- 11. старый сервер --------------------------------------------------
say "11/11  Старый сервер"
if [ $OLD_ALIVE = 1 ] && [ "$OLD_HOST" != "$NEW_HOST" ]; then
    # У части людей ссылка ведёт прямо на старый VPS (http://IP:8080/…).
    # Пусть он отдаёт им подписку с новым адресом: клиенты переедут при
    # обновлении, а старый сервер доживает как указатель.
    if ctl "$OLD_HOST" server "$NEW_HOST" | grep -q '"ok": *true'; then
        ok "$OLD_HOST теперь раздаёт подписки с адресом $NEW_HOST"
    else
        warn "не удалось переписать подписки на $OLD_HOST — у людей со старыми ссылками переезд не случится сам"
    fi
else
    warn "старый сервер недоступен — люди со старыми ссылками http://IP:8080/… останутся без обновления"
fi

# Свежий бэкап состояния уже с нового сервера — на случай следующего переезда.
/usr/local/sbin/vpn-state-backup.sh >/dev/null 2>&1 && ok "состояние сохранено в $BACKUP_DIR" \
    || warn "бэкап состояния не снялся — завтра в 04:30 таймер попробует сам"

cat <<FINAL

Готово. VPN работает на $NEW_HOST.

  • Бот, панель, телевизоры — уже на новом сервере.
  • Люди со ссылками $SUB_BASE/… переедут сами при обновлении подписки
    (клиенты делают это раз в сутки; можно нажать «обновить» в приложении).
  • Люди со старыми ссылками http://$OLD_HOST:8080/… переедут, пока жив
    старый сервер. Держите его ещё ~2 недели, потом гасите.

Откат, пока старый сервер жив:  $0 root@$OLD_HOST
FINAL
