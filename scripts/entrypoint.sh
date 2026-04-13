#!/usr/bin/env bash
set -euo pipefail

# Ensure env vars are exported for Jackett
export HOME="${HOME:-/root}"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-/root/.config}"
export XDG_DATA_HOME="${XDG_DATA_HOME:-/root/.local/share}"
export JACKETT_PORT="${JACKETT_PORT:-9118}"

JACKETT_BIN="/opt/jackett/jackett"
JACKETT_DATA="/data/jackett"
JACKETT_ARGS="--NoRestart --DataFolder ${JACKETT_DATA} --Port ${JACKETT_PORT} --ListenPublic"

echo "━━━ Pirate Dock v2 ━━━"
echo "Base: bubuntux/nordvpn"
echo "API:  http://0.0.0.0:9876"
echo "Jackett: http://0.0.0.0:${JACKETT_PORT}"
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

# Login with token if available
if [ -n "${NORDVPN_TOKEN:-}" ]; then
    nordvpn login --token "$NORDVPN_TOKEN" 2>&1 || true
fi

# VPN connect in background (don't block startup)
(
    echo "[vpn] Connecting to ${NORDVPN_COUNTRY:-South_Africa} P2P..."
    nordvpn connect --group "${NORDVPN_GROUP:-P2P}" "${NORDVPN_COUNTRY:-South_Africa}" 2>&1 || \
    nordvpn connect --group "${NORDVPN_GROUP:-P2P}" 2>&1 || \
    echo "[vpn] VPN connect failed — use POST /vpn/connect to retry"
    echo "[vpn] $(nordvpn status 2>&1)"
) &

# Start Jackett in background
echo "[jackett] Starting Jackett on port ${JACKETT_PORT}..."
$JACKETT_BIN $JACKETT_ARGS &
JACKETT_PID=$!
echo "[jackett] PID: ${JACKETT_PID}"

# Wait for Jackett to be ready
for i in $(seq 1 15); do
    if curl -sf "http://127.0.0.1:${JACKETT_PORT}/api/v1.0/server/config" >/dev/null 2>&1; then
        echo "[jackett] Ready after ${i}s."
        break
    fi
    sleep 1
done

# Start API server
echo "Starting API server..."
exec uvicorn server:app --host 0.0.0.0 --port 9876
