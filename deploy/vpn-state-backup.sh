#!/bin/bash
# Ежедневный бэкап состояния VPN с VPS.
#
# Состояние — это логины с паролями (mieru-server.json) и токены подписок
# (mieru-meta.json). Пока они целы, новый сервер поднимается с теми же
# учётными данными и никого не нужно переподключать. Потеряются — придётся
# обходить всех пятнадцать человек с новыми ссылками.
#
# Ходим тем же forced-command ключом, что и бот: действие state только
# читает и ничего не меняет.

set -euo pipefail

KEY=/home/san/Tg_bot/ssh/id_ed25519_vpn
ENV=/home/san/Tg_bot/.env
OUT=/home/san/vpn-backup
KEEP=14

HOST=$(grep -oP '^VPN_SSH_HOST=\K.*' "$ENV" 2>/dev/null || true)
[ -n "$HOST" ] || { echo "VPN_SSH_HOST не задан в $ENV"; exit 1; }

STAMP=$(date +%Y%m%d)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

ssh -i "$KEY" -n -o BatchMode=yes -o ConnectTimeout=20 "root@$HOST" state \
    > "$TMP/state.json" 2>/dev/null \
    || { echo "VPS $HOST не ответил — прошлый бэкап оставлен нетронутым"; exit 1; }

python3 - "$TMP" <<'PY'
import json, sys, pathlib
tmp = pathlib.Path(sys.argv[1])
d = json.loads((tmp / "state.json").read_text())
if not d.get("ok"):
    raise SystemExit("VPS вернул ошибку: %s" % d.get("error"))
users = d["server"].get("users") or []
tokens = d["meta"].get("tokens") or {}
# Пустой список — почти наверняка сбой на той стороне. Записать такое
# поверх хорошего бэкапа хуже, чем не записать ничего.
if not users or not tokens:
    raise SystemExit("в ответе нет пользователей или токенов — не сохраняю")
(tmp / "mieru-server.json").write_text(json.dumps(d["server"], indent=2, ensure_ascii=False))
(tmp / "mieru-meta.json").write_text(json.dumps(d["meta"], indent=2, ensure_ascii=False))
print("%d пользователей, %d токенов" % (len(users), len(tokens)))
PY

mkdir -p "$OUT/$STAMP"
install -m 600 "$TMP/mieru-server.json" "$TMP/mieru-meta.json" "$OUT/$STAMP/"
chmod 700 "$OUT" "$OUT/$STAMP"

# Держим две недели снимков: их размер — единицы килобайт, зато видно,
# когда именно пропал человек, если доступ отозвали по ошибке.
ls -d "$OUT"/*/ 2>/dev/null | head -n -"$KEEP" | xargs -r rm -rf

echo "сохранено в $OUT/$STAMP"
