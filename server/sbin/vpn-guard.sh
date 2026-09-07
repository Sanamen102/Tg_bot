#!/bin/sh
# Страж tv-vpn: восстанавливает tproxy-маршрутизацию, если слетела
# (systemd-networkd при передёргивании USB-интерфейса сносит "чужие" правила).
if ! nft list table ip vpn >/dev/null 2>&1; then
  systemctl restart vpn-routing
  exit 0
fi
[ "$(ip rule show | grep -c 'fwmark 0x1 lookup 100')" -eq 0 ] && ip rule add fwmark 1 table 100
ip route show table 100 2>/dev/null | grep -q 'local default' || ip route replace local 0.0.0.0/0 dev lo table 100
exit 0
