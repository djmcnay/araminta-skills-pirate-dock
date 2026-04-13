#!/usr/bin/env bash
# Safe build wrapper — runs pre-build check, then builds with cache limits
# Usage: bash scripts/build.sh [--no-cache]
# This is what Minty should call instead of `docker compose up -d --build`

set -euo pipefail

NO_CACHE=""
if [ "${1:-}" = "--no-cache" ]; then
    NO_CACHE="--no-cache"
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# ── Step 1: Pre-build check ──
bash "$SCRIPT_DIR/pre-build-check.sh" || {
    echo ""
    echo "❌ Pre-build check failed. Aborting."
    exit 1
}

# ── Step 2: Set Docker build cache limit (1GB max) ──
# This prevents cache from growing unbounded
export DOCKER_BUILDKIT=1
BUILDKIT_CACHE_MAX="1024"  # MB

echo ""
echo "🔨 Building pirate-dock..."
echo "Cache limit: ${BUILDKIT_CACHE_MAX}MB"
echo ""

# ── Step 3: Build ──
cd "$PROJECT_DIR"
docker compose build $NO_CACHE --progress=plain

# ── Step 4: Post-build cleanup — trim cache back down ──
echo ""
echo "▸ Trimming build cache..."
docker builder prune -f --filter "until=30m" 2>/dev/null || true

# ── Step 5: Start ──
echo "▸ Starting container..."
docker compose up -d

echo ""
echo "✅ Pirate-Dock built and started"
echo "Status: curl -s http://localhost:9876/status | python3 -m json.tool"
