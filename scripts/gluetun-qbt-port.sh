#!/bin/sh
# shellcheck shell=sh
# Push Gluetun's forwarded port into qBittorrent's listen port.
#
# Run by Gluetun via VPN_PORT_FORWARDING_UP_COMMAND every time the VPN provider
# assigns a port. ProtonVPN issues a NEW random port on every reconnect, so a
# hand-set listen port silently goes stale the first time the tunnel flaps and
# inbound peers stop arriving.
#
# Runs inside the Gluetun container (busybox sh, wget, no curl). qBittorrent
# shares Gluetun's network namespace, so its WebUI is reachable on 127.0.0.1 and
# the request arrives as localhost -- which is why qBittorrent's
# "Bypass authentication for clients on localhost" must be enabled. That keeps a
# WebUI credential out of the compose file and the environment entirely.
#
# Usage: gluetun-qbt-port.sh <forwarded-port> [qbittorrent-webui-port]
set -eu

PORT="${1:?forwarded port required}"
QBT_PORT="${2:-6969}"
URL="http://127.0.0.1:${QBT_PORT}/api/v2/app/setPreferences"

# qBittorrent only starts once Gluetun reports healthy, so the first forwarded
# port usually arrives before its WebUI is accepting connections.
attempt=0
while [ "$attempt" -lt 30 ]; do
    if wget -qO- --post-data="json={\"listen_port\":${PORT}}" "$URL" >/dev/null 2>&1; then
        echo "[qbt-port] qBittorrent listen port set to ${PORT}"
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 2
done

echo "[qbt-port] FAILED to set qBittorrent listen port to ${PORT} after 60s" >&2
echo "[qbt-port] check that qBittorrent is running and that localhost auth bypass is on" >&2
exit 1
