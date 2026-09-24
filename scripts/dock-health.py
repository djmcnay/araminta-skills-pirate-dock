#!/usr/bin/env python3
"""dock-health: once-daily zero-token health report for the pirate-dock stack.

Writes its report to the vault (~/araminta-vault/99_system/health-check/)
instead of messaging David — silence on success, recorded everywhere.
Exit 0 when OK, 1 when anything is DEGRADED (cron failure-delivery then
pings Telegram).

Run ON the Pi. Cron (Hermes, --no-agent): daily 04:30.
  hermes cron create "30 4 * * *" --name dock-health-check --no-agent \
      --script dock-health.py --deliver local --failure-deliver telegram
The script must be COPIED (not symlinked) into ~/.hermes/scripts/.

Checks:
  1. Dock container: reachable status endpoint (VPN connected, Jackett up)
     — or idle-by-design if the container is stopped with nothing in flight
  2. aria2 zombies: RPC ports responding from INSIDE the container that
     hold no active download (starvation signature)
  3. Disk: root filesystem and downloads dir size
  4. Autopromote watcher: last cron run status from `hermes cron list`
  5. Watcher journal: error/failure stages in the state file
  6. Jellyfin: HTTP up on 8096
"""
import fcntl
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

DOCK = Path("/home/djmcnay/Documents/GitHub/pirate-dock/downloads")
REPORT_DIR = Path("/home/djmcnay/araminta-vault/99_system/health-check")
REPORT = REPORT_DIR / "pirate-dock.md"
STATE_FILE = Path("/home/djmcnay/.local/state/dock-autopromote.json")
LOG_FILE = Path("/home/djmcnay/.local/state/dock-autopromote.log")
HERMES = "/home/djmcnay/.local/bin/hermes"

problems: list[str] = []
notes: list[str] = []
lines: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "✅" if ok else "❌"
    lines.append(f"- {mark} **{name}**{': ' + detail if detail else ''}")
    if not ok:
        problems.append(f"{name}: {detail}")


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    lock = (Path("/tmp") / "dock-health.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another dock-health instance running; exiting")
        return 0

    # 1. dock container
    container_running = subprocess_status("docker inspect -f {{.State.Running}} pirate-dock") == "true"
    if container_running:
        try:
            raw = urllib.request.urlopen(
                "http://localhost:9876/status", timeout=10).read()
            st = json.loads(raw)
            check("Dock API", True,
                  f"VPN {'up (' + st.get('country', '?') + ')' if st.get('connected') else 'DOWN'}"
                  f", Jackett {'up' if st.get('jackett_running') else 'DOWN'}")
            if not st.get("connected"):
                problems.append("VPN tunnel is DOWN while container is running")
            if not st.get("jackett_running"):
                problems.append("Jackett is DOWN")
        except Exception as e:
            check("Dock API", False, f"unreachable: {e}")
    else:
        in_flight = len(list(DOCK.glob("*.aria2"))) if DOCK.exists() else 0
        check("Dock container", True,
              f"stopped (idle by design); {in_flight} control file(s) in downloads")

    # 2. aria2 zombies (RPC inside the container only — host probes lie)
    if container_running:
        try:
            out = subprocess_status(
                "docker exec pirate-dock python3 -c \""
                "import json,urllib.request\n"
                "live=0;idle=0\n"
                "for port in range(6800,6812):\n"
                "    try:\n"
                "        b=json.dumps({'jsonrpc':'2.0','id':'x','method':'aria2.tellActive','params':[]}).encode()\n"
                "        r=json.load(urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{port}/jsonrpc',data=b,headers={'Content-Type':'application/json'}),timeout=2))\n"
                "        if r.get('result'): live+=1\n"
                "        else: idle+=1\n"
                "    except Exception: pass\n"
                "print(f'{live} {idle}')\"")
            live, idle = (out.split() + ["0", "0"])[:2]
            check("aria2 processes", True,
                  f"{live} active, {idle} idle-but-responding (0 = all clear)")
        except Exception as e:
            check("aria2 processes", False, f"probe failed: {e}")

    # 3. disk
    try:
        out = subprocess_status("df -h /home/djmcnay").splitlines()[-1]
        pct = int(out.split()[4].rstrip("%"))
        check("Disk", pct < 85, f"root at {pct}%")
        if pct >= 85:
            problems.append(f"disk at {pct}%")
    except Exception as e:
        check("Disk", False, str(e))
    if DOCK.exists():
        out = subprocess_run(f"du -sh {DOCK} | cut -f1")
        notes.append(f"downloads dir: {out}")

    # 4. autopromote watcher last cron run
    try:
        listing = subprocess_run(f"{HERMES} cron list")
        block = listing.split("Name:      dock-autopromote")[-1] \
            if "dock-autopromote" in listing else ""
        last = ""
        for ln in block.splitlines():
            if "Last run:" in ln:
                last = ln.strip()
                break
        ok = bool(last) and last.endswith("ok")
        check("autopromote cron", ok, last or "job not found in cron list")
        if not ok:
            problems.append(f"autopromote cron: {last or 'missing'}")
    except Exception as e:
        check("autopromote cron", False, str(e))

    # 4b. Ollama cloud burn-rate (weekly allowance meter)
    try:
        log_path = Path("/home/djmcnay/.hermes/ollama-meter/log.jsonl")
        meter_lines = log_path.read_text().strip().splitlines() \
            if log_path.exists() else []
        if not meter_lines:
            check("Ollama meter", False,
                  "log.jsonl missing or empty — meter capture not running?")
            problems.append("Ollama meter log missing")
        else:
            sample = json.loads(meter_lines[-1])
            weekly = sample.get("weekly_usage")
            week = sample.get("iso_week", "?")
            top = ", ".join(
                f"{m.get('name')}:{m.get('request_count')}"
                for m in (sample.get("weekly_models") or [])[:3])
            check("Ollama meter", True,
                  f"week {week}: usage {weekly} of allowance"
                  + (f"; top models {top}" if top else ""))
            if isinstance(weekly, (int, float)) and weekly > 0.9:
                problems.append(f"Ollama weekly allowance {weekly*100:.0f}% used ({week})")
    except Exception as e:
        check("Ollama meter", False, str(e))

    # 5. watcher journal failure stages
    try:
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        from collections import Counter
        stages = Counter(v.get("stage", "(in flight)") for v in state.values())
        bad = {k: v for k, v in stages.items()
               if k in ("decode-failed", "promote-failed", "skipped-no-video",
                        "superseded-duplicate")}
        check("Watcher journal", True,
              ", ".join(f"{k}={v}" for k, v in sorted(stages.items())))
        if bad:
            notes.append(f"stages needing attention: {bad}")
    except Exception as e:
        check("Watcher journal", False, str(e))

    # 6. Jellyfin
    try:
        urllib.request.urlopen("http://localhost:8096/System/Info/Public", timeout=10)
        check("Jellyfin", True, "responding on 8096")
    except Exception as e:
        check("Jellyfin", False, str(e))
        problems.append("Jellyfin unreachable")

    now = datetime.now().strftime("%Y-%m-%d %H:%M %Z").strip()
    status = "OK" if not problems else "DEGRADED"
    lines_j = "\n".join(lines)
    notes_j = "\n".join(f"- {n}" for n in notes) or "- none"
    probs_j = "\n".join(f"- {p}" for p in problems) or "- none"
    report = f"""---
type: log
last-updated: {now[:10]}
maintained-by: Pi script dock-health.py
---

# Pirate-dock daily health — {now}

**STATUS: {status}**

## Checks
{lines_j}

## Problems
{probs_j}

## Notes
{notes_j}

Machine-generated by `scripts/dock-health.py` (pirate-dock repo). Vault-sync
commits this file; git history is the archive. Zero tokens by design
(--no-agent cron). A human/agent reading this should only act when STATUS is
DEGRADED or a problem line names one.
"""
    tmp = REPORT.with_suffix(".tmp")
    tmp.write_text(report)
    tmp.replace(REPORT)
    print(f"dock-health: {status} — report written to {REPORT}")
    return 0 if not problems else 1


def subprocess_status(cmd: str) -> str:
    import subprocess
    return subprocess.run(["/bin/sh", "-c", cmd], capture_output=True,
                          text=True, timeout=60).stdout.strip()


def subprocess_run(cmd: str) -> str:
    import subprocess
    r = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True,
                       text=True, timeout=120)
    return (r.stdout or r.stderr).strip()


if __name__ == "__main__":
    sys.exit(main())