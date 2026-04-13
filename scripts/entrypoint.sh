#!/usr/bin/env bash
set -euo pipefail

# Ensure env vars are exported for Jackett (bubuntux init may not pass them)
export HOME="${HOME:-/root}"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-/root/.config}"
export XDG_DATA_HOME="${XDG_DATA_HOME:-/root/.local/share}"

echo "━━━ Pirate Dock v2 ━━━"
echo "Base: bubuntux/nordvpn"
echo "API:  http://0.0.0.0:9876"
echo "Jackett: http://0.0.0.0:${JACKETT_PORT:-9118}"
echo "HOME=$HOME"

# Wait for NordVPN daemon
for i in $(seq 1 30); do
    if nordvpn status 2>&1 | grep -q "Status:"; then
        echo "VPN daemon ready after ${i}s."
        break
    fi
    sleep 1
done

# Configure NordVPN
nordvpn set analytics disabled 2>&1 || true
nordvpn set meshnet off 2>&1 || true

# VPN connect in background (don't block startup)
(
    echo "[vpn] Connecting to ${NORDVPN_COUNTRY:-South_Africa} P2P..."
    nordvpn connect --group "${NORDVPN_GROUP:-P2P}" "${NORDVPN_COUNTRY:-South_Africa}" 2>&1 || \
    nordvpn connect --group "${NORDVPN_GROUP:-P2P}" 2>&1 || \
    echo "[vpn] VPN connect failed — use POST /vpn/connect to retry"
    echo "[vpn] $(nordvpn status 2>&1)"
) &

echo "Starting API server..."
exec uvicorn server:app --host 0.0.0.0 --port 9876
