#!/usr/bin/env bash
# Pirate-Dock Docker cleanup — reclaim space safely
# Does NOT touch honcho containers (HA, Redis, Postgres)
# Usage: bash scripts/prune-docker.sh [--aggressive]

set -euo pipefail

AGGRESSIVE="${1:-}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "🧹 Pirate-Dock Docker Cleanup"
echo "=============================="

# ── Show before state ──
echo ""
echo "BEFORE:"
docker system df

echo ""

# ── 1. Stop & remove only pirate-dock container ──
echo "▸ Stopping pirate-dock container (if running)..."
docker compose down 2>/dev/null || true

# ── 2. Remove pirate-dock image specifically ──
echo "▸ Removing pirate-dock image..."
docker rmi pirate-dock-pirate-dock:latest 2>/dev/null || true

# ── 3. Remove pirate-dock dangling/volatile images ──
echo "▸ Removing dangling images..."
docker image prune -f

# ── 4. Build cache (safe to nuke) ──
echo "▸ Clearing build cache..."
docker builder prune -f

# ── 5. Downloaded files in project dir ──
if [ -d "$PROJECT_DIR/downloads" ]; then
    DL_SIZE=$(du -sh "$PROJECT_DIR/downloads" 2>/dev/null | cut -f1)
    echo "▸ Project downloads dir: $DL_SIZE"
    # Keep dir but clean old downloads older than 7 days
    find "$PROJECT_DIR/downloads" -type f -mtime +7 -delete 2>/dev/null || true
fi

# ── Aggressive: remove all unused images ──
if [ "$AGGRESSIVE" = "--aggressive" ]; then
    echo ""
    echo "▸ AGGRESSIVE MODE: removing ALL unused images..."
    docker image prune -af
    docker volume prune -f 2>/dev/null || true
    echo "  ⚠️  Honcho/HA images preserved (currently in use)"
fi

# ── Show after state ──
echo ""
echo "AFTER:"
docker system df

# ── Disk summary ──
USAGE=$(df / | tail -1 | awk '{print $5}' | tr -d '%')
AVAIL=$(df -h / | tail -1 | awk '{print $4}')
echo ""
echo "Disk: ${USAGE}% used (${AVAIL} available)"
echo ""
echo "✅ Done. To rebuild: docker compose up -d --build"
