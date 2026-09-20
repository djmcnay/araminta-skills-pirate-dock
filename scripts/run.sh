#!/usr/bin/env bash
set -euo pipefail

# ── Environment ────────────────────────────────────────────────
export HOME="${HOME:-/root}"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-/root/.config}"
export XDG_DATA_HOME="${XDG_DATA_HOME:-/root/.local/share}"
export JACKETT_PORT="${JACKETT_PORT:-9118}"

# Load bind-mounted .env — avoids s6 init system stripping env vars
if [ -f /app/.env ]; then
    while IFS="=" read -r key value 2>/dev/null || [ -n "$key" ]; do
        [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
        key="${key#export }"
        key="$(echo "$key" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
        value="$(echo "$value" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
        value="${value%\"}"; value="${value#\"}"
        value="${value%\'}"; value="${value#\'}"
        if [ -n "$key" ] && [ -z "${!key:-}" ]; then
            export "$key"="$value"
        fi
    done < /app/.env
    echo "[env] Loaded /app/.env"
fi

# Read NordVPN token from bind-mounted file (avoids s6 env var stripping)
TOKEN_FILE="/run/pirate-dock/token"
if [ -f "$TOKEN_FILE" ]; then
    NORDVPN_TOKEN=$(tr -d '\n\r\t\"' < "$TOKEN_FILE")
    echo "[vpn] Token loaded from file (${#NORDVPN_TOKEN} chars)."
else
    echo "[vpn] WARNING: No token file found at $TOKEN_FILE"
fi

JACKETT_BIN="/opt/jackett/jackett"
JACKETT_DATA="/data/jackett"

# ── Display / noVNC stack ────────────────────────────────────────
# Xvfb :1      — virtual framebuffer (no physical display needed)
# x11vnc       — exports Xvfb display as VNC on localhost:5900
# websockify   — bridges VNC to WebSocket on 0.0.0.0:6081, serves noVNC HTML
# noVNC        — HTML5 VNC client; David opens vnc_lite.html?path=pirate%2F
echo "[display] Starting Xvfb + x11vnc + websockify..."
export DISPLAY=:1
Xvfb :1 -screen 0 1280x800x24 -ac +extension GLX +render -noreset &
sleep 1
x11vnc -display :1 -forever -shared -localhost -nopw -noxdamage -noxfixes &
sleep 1
# In-container 0.0.0.0 is correct here: Docker's port publisher connects from
# the bridge gateway, not container-loopback (loopback bind => funnel 502).
# The real boundary is the HOST-side publish: 127.0.0.1:6082 in compose.
websockify 0.0.0.0:6081 localhost:5900 --web=/usr/share/novnc &
DISPLAY_URL="${DISPLAY_URL:-https://araminta.taild3f7b9.ts.net/pirate/vnc_lite.html?path=pirate%2F}"
echo "[display] noVNC ready: $DISPLAY_URL"

# ── Persistent Chromium (CDP on :9223) ─────────────────────────
# Launches a single long-lived Chromium instance so Minty can
# connect via CDP without spawning fresh browsers per request.
# Auto-discovers the Playwright-installed Chromium binary.
# Wrapped in a watchdog: if Chromium crashes (OOM, segfault, etc.)
# it is automatically relaunched so the display stack never sits empty.
CHROME_CDP_PORT=9223
CHROMIUM_BIN=$(find /root/.cache/ms-playwright -name chrome -type f -executable 2>/dev/null | head -1)

watchdog_chrome() {
    local restart_count=0
    while true; do
        if [ "$restart_count" -gt 0 ]; then
            echo "[browser] Relaunching Chromium (restart #${restart_count})..."
            sleep 2  # Brief cooldown before restart
        else
            echo "[browser] Launching persistent Chromium with CDP on :${CHROME_CDP_PORT}..."
        fi

        "$CHROMIUM_BIN" \
            --no-sandbox --disable-gpu --disable-dev-shm-usage \
            --disable-blink-features=AutomationControlled \
            --window-size=1280,800 \
            --remote-debugging-port=${CHROME_CDP_PORT} \
            --remote-debugging-address=127.0.0.1 \
            --no-first-run --disable-default-apps \
            --disable-popup-blocking --disable-translate \
            "about:blank" &
        CHROMIUM_PID=$!

        # Wait for CDP to be reachable
        local ready=0
        for i in $(seq 1 10); do
            if curl -sf "http://127.0.0.1:${CHROME_CDP_PORT}/json/version" >/dev/null 2>&1; then
                echo "[browser] Chromium CDP ready after ${i}s (PID ${CHROMIUM_PID})."
                ready=1
                break
            fi
            sleep 1
        done
        [ "$ready" -eq 0 ] && echo "[browser] WARNING: CDP not ready after 10s (PID ${CHROMIUM_PID})"

        # Block until Chromium exits, then loop to restart.
        # `wait || true` is load-bearing: under set -e, a bare `wait` on a
        # nonzero child exit (crash, OOM-kill, SIGTERM) aborts this function
        # and silently kills the watchdog — the exact failure that made both
        # watchdogs dead code before (reproduced 2026-09-20).
        wait $CHROMIUM_PID 2>/dev/null || true
        restart_count=$((restart_count + 1))
        echo "[browser] Chromium exited (PID ${CHROMIUM_PID}, restart #${restart_count}). Relaunching..."
    done
}

if [ -x "$CHROMIUM_BIN" ]; then
    echo "[browser] Chromium binary found; will launch after VPN connect (fail-closed ordering)."
else
    echo "[browser] WARNING: Chromium binary not found at $CHROMIUM_BIN"
fi

echo "━━━ Pirate Dock v3.3 ━━━"
echo "API:     http://0.0.0.0:9876"
echo "Jackett: http://0.0.0.0:${JACKETT_PORT}"
echo "CDP:     http://0.0.0.0:${CHROME_CDP_PORT}"
echo "Display: $DISPLAY_URL"

# ── Wait for NordVPN daemon (started by s6 /init) ─────────────
echo "[vpn] Waiting for NordVPN daemon..."
for i in $(seq 1 60); do
    if [ -S /run/nordvpn/nordvpnd.sock ] && nordvpn status 2>&1 | grep -q "Status:"; then
        echo "[vpn] Daemon ready after ${i}s."
        break
    fi
    sleep 1
    [ "$i" -eq 60 ] && echo "[vpn] WARNING: Daemon timeout; continuing with best effort"
done

# ── Configure NordVPN ─────────────────────────────────────────
nordvpn set analytics disabled 2>&1 || true
nordvpn set meshnet off 2>&1 || true
nordvpn set killswitch on 2>&1 || true

# ── Login ──────────────────────────────────────────────────────
if [ -n "${NORDVPN_TOKEN:-}" ]; then
    echo "[vpn] Authenticating..."
    nordvpn login --token "$NORDVPN_TOKEN" 2>&1 && echo "[vpn] Login OK." || echo "[vpn] Login FAILED."
fi

# ── Auto-whitelist Docker bridge so host can reach API / Jackett / xpra ─
if command -v ip >/dev/null 2>&1; then
    BRIDGE_SUBNET=$(ip -4 route | awk '/default/ {next} /docker0|br-/ {print $1}' | head -1)
    if [ -n "$BRIDGE_SUBNET" ] && [ "$BRIDGE_SUBNET" != "0.0.0.0/0" ] && [ "$BRIDGE_SUBNET" != "127.0.0.0/8" ]; then
        echo "[vpn] Whitelisting Docker subnet: $BRIDGE_SUBNET"
        nordvpn whitelist add subnet "$BRIDGE_SUBNET" 2>&1 || true
    else
        echo "[vpn] Fallback: whitelisting 172.16.0.0/12"
        nordvpn whitelist add subnet 172.16.0.0/12 2>&1 || true
    fi
else
    nordvpn whitelist add subnet 172.16.0.0/12 2>&1 || true
fi
nordvpn whitelist add subnet 127.0.0.0/8 2>/dev/null || true
nordvpn whitelist add port 9876 2>/dev/null || true
nordvpn whitelist add port 9118 2>/dev/null || true
nordvpn whitelist add port 6081 2>/dev/null || true
nordvpn whitelist add port 9223 2>/dev/null || true

watchdog_jackett() {
    local restart_count=0
    while true; do
        if [ "$restart_count" -gt 0 ]; then
            echo "[jackett] Relaunching (restart #${restart_count})..."
            sleep 5  # Cooldown — Jackett releases ports slowly
        else
            echo "[jackett] Starting..."
        fi

        $JACKETT_BIN --NoRestart --DataFolder "$JACKETT_DATA" \
            --Port "$JACKETT_PORT" --ListenPublic &
        JACKETT_PID=$!

        local ready=0
        for i in $(seq 1 30); do
            if curl -sf "http://127.0.0.1:${JACKETT_PORT}/api/v1.0/server/config" \
                >/dev/null 2>&1; then
                echo "[jackett] Ready after ${i}s (PID ${JACKETT_PID})."
                ready=1
                break
            fi
            sleep 1
        done
        [ "$ready" -eq 0 ] && echo "[jackett] WARNING: Not ready after 30s (PID ${JACKETT_PID})"

        # Same set-e guard as the Chromium watchdog: without `|| true` a
        # SIGKILL (crash or /jackett/restart) aborts the watchdog itself.
        wait $JACKETT_PID 2>/dev/null || true
        restart_count=$((restart_count + 1))
        echo "[jackett] Exited (PID ${JACKETT_PID}, restart #${restart_count}). Relaunching..."
    done
}

# ── Connect VPN (synchronous — browser/Jackett must NOT start before tunnel) ──
# Before this change, Chromium launched at the top of the script and its
# traffic could exit via the host IP during the connect window.
GROUP="${NORDVPN_GROUP:-P2P}"
TECH="${NORDVPN_TECHNOLOGY:-NordLynx}"
COUNTRY="${NORDVPN_COUNTRY:-${CONNECT:-South_Africa}}"
VPN_UP=0
nordvpn set technology "$TECH" 2>&1 || true
for attempt in 1 2 3; do
    echo "[vpn] Connect attempt ${attempt} -> $COUNTRY ($GROUP)..."
    if nordvpn connect --group "$GROUP" "$COUNTRY" 2>&1; then
        for i in $(seq 1 20); do
            if nordvpn status 2>&1 | grep -q "Connected"; then
                VPN_UP=1
                break
            fi
            sleep 2
        done
        [ "$VPN_UP" -eq 1 ] && break
    fi
    sleep 3
done
echo "[vpn] $(nordvpn status 2>&1 | head -3)"
if [ "$VPN_UP" -ne 1 ]; then
    # Fail-closed: no browser, no Jackett until a tunnel exists.
    # The API still comes up (below) so POST /vpn/connect can retry; a small
    # attendant starts browser+Jackett the moment a connection appears.
    echo "[vpn] FATAL: no tunnel after 3 attempts — browser/Jackett withheld (fail-closed)."
    (
        while true; do
            sleep 30
            if nordvpn status 2>&1 | grep -q "Connected"; then
                echo "[vpn] Tunnel detected — starting browser + Jackett."
                watchdog_chrome &
                watchdog_jackett &
                break
            fi
        done
    ) &
else
    watchdog_chrome &
    watchdog_jackett &
fi

# ── FastAPI server (foreground — keeps container alive) ────────
echo "[api] Starting FastAPI..."
exec uvicorn server:app --host 0.0.0.0 --port 9876
