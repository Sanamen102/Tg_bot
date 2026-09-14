#!/usr/bin/env bash
# Глушит QUIC (UDP/443) к сетям Google, чтобы клиенты уходили на TCP,
# где работает обход DPI. Провайдер режет QUIC, обмануть его нечем —
# поэтому дешевле сразу отказать, чем ждать таймаута.
set -euo pipefail
LIST=/etc/quicblock/google-nets.txt
mkdir -p /etc/quicblock

# Пытаемся обновить список префиксов Google; если не вышло — работаем на сохранённом,
# чтобы сбой сети не оставил дом без правила.
tmp=$(mktemp)
if curl -s --max-time 60 \
     'https://stat.ripe.net/data/announced-prefixes/data.json?resource=AS15169' \
   | python3 -c '
import sys, json, ipaddress
d = json.load(sys.stdin)
raw = [ipaddress.ip_network(p["prefix"]) for p in d["data"]["prefixes"] if ":" not in p["prefix"]]
print("\n".join(str(n) for n in sorted(ipaddress.collapse_addresses(raw))))
' > "$tmp" 2>/dev/null && [ -s "$tmp" ]; then
    mv "$tmp" "$LIST"
    echo "список префиксов обновлён"
else
    rm -f "$tmp"
    echo "обновить список не удалось — использую сохранённый"
fi

[ -s "$LIST" ] || { echo "нет списка префиксов, нечего применять"; exit 1; }

nets=$(paste -sd, "$LIST")
nft delete table ip quicblock 2>/dev/null || true
nft -f - <<NFT
table ip quicblock {
  set google {
    type ipv4_addr
    flags interval
    elements = { $nets }
  }
  chain forward {
    type filter hook forward priority 0; policy accept;
    udp dport 443 ip daddr @google counter reject with icmp type port-unreachable
  }
}
NFT
echo "quicblock применён, префиксов: $(wc -l < "$LIST")"
