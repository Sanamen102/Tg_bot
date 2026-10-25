# Telegram DC5 через Cloudflare WARP

С VPS у Аезы (147.45.72.72) не открывается TCP к пятому дата-центру Telegram
(Сингапур: `91.108.56.0/22`, `149.154.171.0/24`). Пинг доходит, TCP-трассировка
обрывается сразу за сетью Аезы. Сам DC5 жив: check-host открывает его из
большинства стран. У пользователей VPN из-за этого бесконечно грузятся фото,
видео и файлы, которые лежат на DC5. Найдено и обойдено 25.10.2026.

## Как устроено

```
клиент → mita → (правило egress: DC5) → warp-relay 127.0.0.1:40001 → WARP 127.0.0.1:40000 → DC5
клиент → mita → всё остальное напрямую, как раньше
```

- **WARP** — официальный клиент Cloudflare в режиме прокси. Маршруты сервера
  он не трогает: добавляет одно правило `ip rule` на несуществующую таблицу,
  оно безвредно.
- **warp-relay** — Xray-посредник. SOCKS5 у WARP теряет запрос, если
  приветствие и CONNECT пришли одним пакетом, а mita шлёт именно так.
  Без посредника mita подключается к WARP и висит до таймаута.
- **Правило в mita** — секция `egress` в `/root/mieru-server.json`.
  `mieru-user` сохраняет конфиг целиком, так что заведение и удаление людей
  правило не стирает.

## Установка на новый сервер

Сначала проверить, нужно ли это вообще: с нового VPS DC5 может открываться и напрямую.

```bash
curl -s -o /dev/null -m 10 -w '%{http_code}\n' http://91.108.56.130/   # 404 — открывается, ставить не нужно
```

Если `000`:

```bash
# 1. WARP: официальный репозиторий, режим прокси ДО подключения —
#    в обычном режиме он заберёт весь трафик сервера вместе с SSH
curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --yes --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" > /etc/apt/sources.list.d/cloudflare-client.list
apt-get update && apt-get install -y cloudflare-warp
warp-cli --accept-tos registration new      # принимает условия Cloudflare
warp-cli --accept-tos mode proxy
warp-cli --accept-tos proxy port 40000
warp-cli --accept-tos connect

# 2. Посредник
mkdir -p /opt/warp-relay   # сюда config.json и run.sh из этого каталога
sh /opt/warp-relay/run.sh

# 3. Правило в mita: добавить в /root/mieru-server.json
#    "egress": {
#      "proxies": [{"name": "warp", "protocol": "SOCKS5_PROXY_PROTOCOL", "host": "127.0.0.1", "port": 40001}],
#      "rules": [{"ipRanges": ["91.108.56.0/22", "149.154.171.0/24"], "action": "PROXY", "proxyNames": ["warp"]}]
#    }
mita apply config /root/mieru-server.json
mita stop && mita start    # egress вступает в силу только после перезапуска, VPN мигнёт на секунды
```

`vps-migrate.sh` секцию `egress` при переезде снимает: на чистом сервере нет
ни WARP, ни посредника, и правило отправило бы DC5 в пустоту.

## Проверка и откат

Из дома, через мост телевизоров:

```bash
curl -s -o /dev/null -m 10 --socks5 127.0.0.1:1081 -w '%{http_code}\n' http://91.108.56.130/   # 404 — работает
```

Откат на VPS: убрать `egress` из `/root/mieru-server.json`, затем
`mita apply config` с `"egress": {}` (apply сливает конфиги, просто убрать
ключ мало) и `mita stop && mita start`. Затем `docker rm -f warp-relay` и
`apt-get purge cloudflare-warp`. Копия конфига до изменений —
`/root/mieru-server.json.bak-20261025`, готовый файл отката — `/root/mieru-rollback.json`.
Оба годятся, только пока состав людей не менялся: в них список пользователей
на 25.10.2026, и `apply` вернул бы его.
