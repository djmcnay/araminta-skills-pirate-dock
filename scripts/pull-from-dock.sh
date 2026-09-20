#!/usr/bin/env bash
# pirate-dock pull: transfer completed downloads from the Pi dock to this Mac.
# Canonical location: pirate-dock repo, scripts/pull-from-dock.sh (this file).
# Clone the repo (or fetch this script) on any machine that should receive
# dock downloads; it needs only: ssh alias `araminta` (Tailscale) + rsync.
#
# Resumable (--partial): safe to re-run after lid-close, network drop, Ctrl-C.
# Verify: byte sizes are compared against the Pi before reporting delivered.
#
# Usage:
#   bash scripts/pull-from-dock.sh                  # default: "UFC 331"
#   bash pull-from-dock.sh "Fireman Sam Series 1-5" # any /downloads item
#
set -uo pipefail

SRC_ROOT="/home/djmcnay/Documents/GitHub/pirate-dock/downloads"
DEST_ROOT="$HOME/Downloads"
ITEM="${1:-UFC 331}"

echo "── Pulling '$ITEM' from dock ──────────────────────────────"
SRC="$SRC_ROOT/$ITEM/"
DEST="$DEST_ROOT/$ITEM/"

# 1. Source inventory (sizes on Pi)
echo "Source (Pi):"
ssh araminta "stat -c '%s  %n' '$SRC'*" 2>/dev/null || { echo "ERROR: nothing at $SRC (is the dock's download finished?)"; exit 1; }

mkdir -p "$DEST"

# 2. Transfer (resumable; rsync delta-verify against existing partials)
# Note: the remote path must be quoted INSIDE the remote arg (ssh-side shell);
# the trailing /* glob expands after ssh resolves the quoted directory.
rsync -av --partial --timeout=60 \
    "araminta:'$SRC'*" "$DEST"
RC=$?
echo "---rsync-exit:$RC---"
[ $RC -ne 0 ] && { echo "rsync failed (exit $RC) — re-run this script to resume."; exit $RC; }

# 3. Verify byte sizes source vs destination
echo "Verifying sizes..."
FAIL=0
while IFS= read -r line; do
    [ -z "$line" ] && continue
    SZ="${line%%  *}"
    FN="${line#*  }"
    BASENAME="$(basename "$FN")"
    LOCAL_SIZE=$(stat -f %z "$DEST$BASENAME" 2>/dev/null || echo 0)
    if [ "$SZ" = "$LOCAL_SIZE" ]; then
        echo "  OK  $BASENAME ($SZ bytes)"
    else
        echo "  MISMATCH  $BASENAME (pi=$SZ mac=${LOCAL_SIZE:-missing})"
        FAIL=1
    fi
done < <(ssh araminta "stat -c '%s  %n' '$SRC'*" 2>/dev/null)

if [ $FAIL -eq 0 ]; then
    echo "DELIVERED: all files verified against the Pi."
    ls -la "$DEST"
else
    echo "SIZE MISMATCH — re-run to repair."
    exit 1
fi