#!/bin/bash
export DISPLAY=:1

# Kill any existing chromium
echo "[test] Killing stale chromium..."
killall -9 chromium-browser chromium 2>/dev/null
sleep 1

# Start chromium on the container display served by Xpra
echo "[test] Starting chromium on DISPLAY=$DISPLAY..."
chromium-browser \
    --no-sandbox \
    --disable-gpu \
    --disable-software-rasterizer \
    --disable-dev-shm-usage \
    --window-size=1280,720 \
    --test-type \
    --user-data-dir=/tmp/chrome-test \
    --no-first-run \
    --no-default-browser-check \
    --disable-background-timer-throttling \
    --disable-backgrounding-occluded-windows \
    --disable-renderer-backgrounding \
    'https://news.ycombinator.com' \
    >/tmp/chromium.log 2>&1 &
CHROME_PID=$!
sleep 5

echo "=== PROCESSES ==="
ps -eo pid,comm,args | grep -i 'chromium\|chrome' | grep -v grep | head -10

echo "=== XWININFO ==="
xwininfo -tree -root 2>/dev/null | grep -iE 'chromium|chrome|Window id' | head -10 || echo 'xwininfo failed'

echo "=== CHROMIUM LOG (last 20 lines) ==="
tail -20 /tmp/chromium.log 2>/dev/null

echo ""
echo "Chromium PID: $CHROME_PID"
echo "Done."
