#!/usr/bin/env bash
# pirate-dock guardrail — run BEFORE building to prevent disk bloat
# Usage: bash scripts/pre-build-check.sh

set -euo pipefail

WARN_THRESHOLD=70   # Warn if disk usage above this %
FAIL_THRESHOLD=85    # Refuse to build if disk usage above this %
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_NAME="pirate-dock-pirate-dock"

echo "🏴‍☠️ Pirate-Dock Pre-Build Check"
echo "================================"

# ── Disk space ──
USAGE=$(df / | tail -1 | awk '{print $5}' | tr -d '%')
AVAIL=$(df -h / | tail -1 | awk '{print $4}')

echo "Disk usage: ${USAGE}% (${AVAIL} available)"

if [ "$USAGE" -ge "$FAIL_THRESHOLD" ]; then
    echo ""
    echo "❌ REFUSING TO BUILD — disk at ${USAGE}% (limit: ${FAIL_THRESHOLD}%)"
    echo ""
    echo "Run cleanup first:"
    echo "  bash scripts/prune-docker.sh    # Clean Docker junk"
    echo "  docker system prune -af          # Nuclear option"
    echo ""
    exit 1
elif [ "$USAGE" -ge "$WARN_THRESHOLD" ]; then
    echo "⚠️  WARNING: Disk at ${USAGE}% — building may push you over the edge"
fi

# ── Existing image size ──
EXISTING=$(docker image inspect "$IMAGE_NAME:latest" --format='{{.Size}}' 2>/dev/null || echo "0")
EXISTING_MB=$((EXISTING / 1024 / 1024))
if [ "$EXISTING_MB" -gt 0 ]; then
    echo "Existing image: ${EXISTING_MB}MB"
    echo "  (rebuild will temporarily use ~2x this during layer transfer)"
fi

# ── Docker system summary ──
echo ""
echo "Docker system summary:"
docker system df 2>/dev/null | grep -E "^(Images|Containers|Build|Total)"

# ── Build cache ──
CACHE_SIZE=$(docker system df 2>/dev/null | grep "Build Cache" | awk '{print $3}' || echo "0B")
echo "Build cache: ${CACHE_SIZE}"

echo ""
echo "✅ Check passed — safe to build"
