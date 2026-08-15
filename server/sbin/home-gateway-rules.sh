#!/bin/sh
set -e

# NAT через nftables
if nft list table ip home_gateway >/dev/null 2>&1; then
    nft delete table ip home_gateway
fi

nft -f /etc/nftables-home-gateway.nft

# FORWARD allow через iptables, чтобы не конфликтовать с Docker
iptables -C FORWARD -i enp3s0 -o enp3s0 -s 192.168.101.0/24 ! -d 192.168.101.0/24 -j ACCEPT 2>/dev/null || \
iptables -I FORWARD 1 -i enp3s0 -o enp3s0 -s 192.168.101.0/24 ! -d 192.168.101.0/24 -j ACCEPT

iptables -C FORWARD -i enp3s0 -o enp3s0 -d 192.168.101.0/24 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || \
iptables -I FORWARD 1 -i enp3s0 -o enp3s0 -d 192.168.101.0/24 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
