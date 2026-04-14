#!/usr/bin/env bash
set -euo pipefail

# ── Environment ────────────────────────────────────────────────
export HOME="${HOME:-/root}"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-/root/.config}"
export XDG_DATA_HOME="${XDG_DATA_HOME:-/root/.local/share}"
export JACKETT_PORT="${JACKETT_PORT:-9118}"

# Read token from bind-mounted file (avoids s6 env var stripping)
TOKEN_FILE="/run/pirate-dock/token"
if [ -f "$TOKEN_FILE" ]; then
    NORDVPN_TOKEN="$(cat "$TOKEN_FILE" | tr -d '[:space:]')"
    echo "[vpn] Token loaded from file (${#NORDVPN_TOKEN} chars)."
else
    # Fallback to env var (in case s6 passes it through)
    NORDVPN_TOKEN="${NORDVPN_TOKEN:-${TOKEN:-}}"
    if [ -n "${NORDVPN_TOKEN:-}" ]; then
        echo "[vpn] Token loaded from env var."
    else
        echo "[vpn] WARNING: No token found!"
    fi
fi

JACKETT_BIN="/opt/jackett/jackett"
JACKETT_DATA="/data/jackett"

echo "━━━ Pirate Dock v2 ━━━"
echo "API:      http://0.0.0.0:9876"
echo "Jackett:  http://0.0.0.0:${JACKETT_PORT}"

# ── Wait for NordVPN daemon (started by s6 /init) ─────────────
echo "[vpn] Waiting for NordVPN daemon..."
for i in $(seq 1 30); do
    if nordvpn status 2>&1 | grep -q "Status:"; then
        echo "[vpn] Daemon ready after ${i}s."
        break
    fi
    sleep 1
    [ "$i" -eq 30 ] && echo "[vpn] WARNING: Daemon timeout"
done

# ── Configure ─────────────────────────────────────────────────
nordvpn set analytics disabled 2>&1 || true
nordvpn set meshnet off 2>&1 || true

# ── Login (foreground — auth state persists to daemon) ────────
if [ -n "${NORDVPN_TOKEN:-}" ]; then
    echo "[vpn] Authenticating..."
    if nordvpn login --token "$NORDVPN_TOKEN" 2>&1; then
        echo "[vpn] Login OK."
    else
        echo "[vpn] Login FAILED."
    fi
fi

# ── Connect (background — don't block Jackett/API) ────────────
(
    COUNTRY="${NORDVPN_COUNTRY:-South_Africa}"
    GROUP="${NORDVPN_GROUP:-P2P}"
    TECH="${NORDVPN_TECHNOLOGY:-NordLynx}"

    sleep 2
    nordvpn set technology "$TECH" 2>&1 || true
    sleep 1
    echo "[vpn] Connecting to $COUNTRY ($GROUP)..."

    nordvpn connect --group "$GROUP" "$COUNTRY" 2>&1 || \
    nordvpn connect --group "$GROUP" 2>&1 || \
    echo "[vpn] VPN connect failed — use POST /vpn/connect to retry"

    echo "[vpn] $(nordvpn status 2>&1)"
) &

# ── Start Jackett ─────────────────────────────────────────────
echo "[jackett] Starting..."
$JACKETT_BIN --NoRestart --DataFolder $JACKETT_DATA --Port $JACKETT_PORT --ListenPublic &
for i in $(seq 1 15); do
    curl -sf "http://127.0.0.1:${JACKETT_PORT}/api/v1.0/server/config" >/dev/null 2>&1 && {
        echo "[jackett] Ready after ${i}s."
        break
    }
    sleep 1
done

# ── Start API server ──────────────────────────────────────────
echo "Starting API server..."
exec uvicorn server:app --host 0.0.0.0 --port 9876
