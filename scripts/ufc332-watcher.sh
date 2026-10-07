#!/bin/bash
# Promote both UFC 332 downloads into Jellyfin as each completes:
#  - "UFC 332" (720p full card: Prelims.mkv + Main Card.mkv)
#  - the KNUCKLES 1080p main-card release (name assigned by aria2 from the torrent)
# Completion test: matching aria2c process gone AND no .aria2 control file left.
# Prints PROMOTED <item> / PROMOTE_FAILED <item> lines; exits when both handled.
DL=/home/djmcnay/Documents/GitHub/pirate-dock/downloads
PROMOTE=/home/djmcnay/Documents/GitHub/pirate-dock/scripts/promote-to-media.py

HASHES=("034307E6418A8C5C0A6759F8BE8F48A2DAF1C4C2" "261ADBEB6F0B9FA8BB90C0F4B282DEEE07569F7C")
LABELS=("720p-full-card" "1080p-main-card")
handled=("" "")

item_for_hash() {  # find the /downloads entry belonging to a magnet hash
  local h_lc=$(echo "$1" | tr 'A-Z' 'a-z')
  docker exec pirate-dock grep -l "$h_lc" /downloads/.aria2-*.log >/dev/null 2>&1
  # fall back: newest matching file/dir by known names
  echo ""
}

while :; do
  for i in 0 1; do
    [[ -n "${handled[$i]}" ]] && continue
    h=${HASHES[$i]}
    # still running?
    if docker exec pirate-dock pgrep -af aria2c 2>/dev/null | grep -qi "$h"; then
      continue
    fi
    # process gone: locate the produced item(s)
    case $i in
      0) cand="$DL/UFC 332" ;;
      1) cand=$(ls -d "$DL"/*KNUCKLES* 2>/dev/null | grep -v '\.aria2$' | head -1) ;;
    esac
    [[ -z "$cand" || ! -e "$cand" ]] && { echo "NOT_FOUND ${LABELS[$i]}"; handled[$i]="missing"; continue; }
    # aria2 control file for this item still present = paused/incomplete, not finished
    ctrl="${cand}.aria2"
    [[ -f "$ctrl" ]] && { echo "WAITING ${LABELS[$i]} (control file present)"; sleep 30; continue; }
    name=$(basename "$cand")
    out=$(python3 "$PROMOTE" "$name" 2>&1); rc=$?
    echo "PROMOTE_BEGIN ${LABELS[$i]} item=\"$name\" rc=$rc"
    echo "$out" | tail -15
    if [[ $rc -eq 0 ]]; then echo "PROMOTED ${LABELS[$i]}"; handled[$i]="ok"; else echo "PROMOTE_FAILED ${LABELS[$i]}"; handled[$i]="error"; fi
  done
  [[ -n "${handled[0]}" && -n "${handled[1]}" ]] && break
  [[ ${SECONDS} -gt 21600 ]] && { echo "WATCHER_TIMEOUT handled=${handled[*]}"; break; }
  sleep 30
done
echo "WATCHER_DONE handled=${handled[*]}"
