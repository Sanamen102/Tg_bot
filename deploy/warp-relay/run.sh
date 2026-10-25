#!/bin/sh
# Посредник между mita и WARP: SOCKS5-прокси Cloudflare WARP теряет запрос,
# если приветствие и CONNECT пришли одним пакетом (а mita шлёт именно так).
# Xray принимает пачкой и передаёт в WARP по шагам. Слушает только 127.0.0.1:40001.
# Пересоздать: sh /opt/warp-relay/run.sh
docker rm -f warp-relay >/dev/null 2>&1 || true
exec docker run -d --name warp-relay --restart unless-stopped --network host \
  -v /opt/warp-relay/config.json:/etc/xray/config.json:ro \
  ghcr.io/xtls/xray-core:latest run -c /etc/xray/config.json
