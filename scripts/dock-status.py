#!/usr/bin/env python3
"""Reliable, conservative torrent-status table for pirate-dock.

On the Pi:  python3 ~/Documents/GitHub/pirate-dock/scripts/dock-status.py
Options:    --watch   refresh every 10s
            --json    machine-readable
            --selftest  offline checks with mocked /queue JSON (no network)
From Mac:   ssh araminta "python3 ~/Documents/GitHub/pirate-dock/scripts/dock-status.py"

Per-item telemetry resolves from GET /queue first (exact name/path match only),
falling back to the strict aria2 RPC payload-path match only for rows /queue
cannot explain.

The table intentionally reports unknown values as '—'.  It never matches jobs
by fuzzy release names, filesystem allocation size, log mtime, or process I/O.
"""
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib import request

DOWNLOADS = Path("/home/djmcnay/Documents/GitHub/pirate-dock/downloads")
API = "http://localhost:9876"
OBSERVATIONS = DOWNLOADS / ".dock-status-observations.json"
RPC_SECRET = "piratedockrpc"


def curl_json(path, timeout=8):
    try:
        with request.urlopen(f"{API}{path}", timeout=timeout) as response:
            return json.loads(response.read())
    except Exception as exc:
        return {"_error": str(exc)}


def human(value):
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(value) < 1024:
            return f"{int(value)} B" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def age(value):
    seconds = max(0, int(time.time() - value))
    if seconds < 120:
        return f"{seconds}s ago"
    if seconds < 7200:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def stamp(value):
    return datetime.fromtimestamp(value).strftime("%H:%M %d %b")


def read_observations():
    try:
        data = json.loads(OBSERVATIONS.read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def write_observations(data):
    temporary = OBSERVATIONS.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(data, sort_keys=True))
        temporary.replace(OBSERVATIONS)
    except OSError:
        # Status must still be usable if an old root-owned downloads mount blocks
        # persistence; TIME then remains conservative rather than fabricated.
        temporary.unlink(missing_ok=True)


def rpc_active_jobs():
    """Query all reserved per-job aria2 endpoints inside the unexposed container."""
    probe = r'''import json, urllib.request
jobs=[]
for port in range(6800, 6900):
    try:
        body=json.dumps({"jsonrpc":"2.0","id":"status","method":"aria2.tellActive","params":["token:piratedockrpc"]}).encode()
        req=urllib.request.Request(f"http://127.0.0.1:{port}/jsonrpc",data=body,headers={"Content-Type":"application/json"},method="POST")
        result=json.loads(urllib.request.urlopen(req,timeout=.25).read()).get("result",[])
        for job in result:
            job["_port"]=port
            jobs.append(job)
    except Exception:
        pass
print(json.dumps(jobs))'''
    try:
        result = subprocess.run(
            ["docker", "exec", "pirate-dock", "python3", "-c", probe],
            capture_output=True, text=True, timeout=35, check=False,
        )
        parsed = json.loads(result.stdout)
        return parsed if isinstance(parsed, list) else []
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []


def job_paths(job):
    return [entry.get("path", "") for entry in job.get("files", []) if entry.get("path")]


def path_matches_job(path, job):
    """Exact payload-path relationship only; no title guessing or list-order fallback."""
    root = str(path)
    if path.is_dir():
        root += "/"
        return any(candidate.startswith(root) for candidate in job_paths(job))
    return root in job_paths(job)


def has_control(path):
    sibling = path.with_name(path.name + ".aria2")
    if sibling.exists():
        return True
    return path.is_dir() and any(path.rglob("*.aria2"))


def size(path):
    if path.is_file():
        return path.stat().st_size
    return sum(child.stat().st_size for child in path.rglob("*") if child.is_file() and child.suffix != ".aria2")


def update_observation(job, records, observations, now):
    gid = job.get("gid")
    if not gid:
        return None
    key = f"{job.get('_port', '?')}:{gid}"
    item = observations.setdefault(key, {})
    record = records.get(str(job.get("_port")), {})
    item.setdefault("started_at", record.get("started_at", now))
    completed = int(job.get("completedLength", 0) or 0)
    speed = int(job.get("downloadSpeed", 0) or 0)
    previous = int(item.get("completed_length", completed))
    if speed > 0 or completed > previous:
        item["last_data_at"] = now
    item["completed_length"] = completed
    item["seen_at"] = now
    return item


def load_records():
    response = curl_json("/downloads/status-jobs")
    return response.get("jobs", {}) if isinstance(response, dict) and "_error" not in response else {}


# ── GET /queue first ─────────────────────────────────────────
# Contract: repo QUEUE.md + scripts/server.py get_queue/_queue_item. Each row
# has name, status (downloading/completed/seeding/stopped), progress
# {downloaded,total,percent}, download_speed, connections, error,
# telemetry_available, files [{path (/downloads-relative), length, completed}].


def load_queue():
    """Fetch GET /queue rows. Returns list, [] when empty, None when unevidenced (fetch error)."""
    response = curl_json("/queue")
    if not isinstance(response, dict) or "_error" in response:
        return None
    jobs = response.get("jobs", [])
    return jobs if isinstance(jobs, list) else None


def normalize_queue_path(value):
    """Strip only the known /downloads prefix; otherwise exact (no fuzzying)."""
    if not isinstance(value, str) or not value:
        return ""
    text = value
    if text.startswith("/downloads/"):
        text = text[len("/downloads/"):]
    elif text.startswith("downloads/"):
        text = text[len("downloads/"):]
    return text.lstrip("/")


def queue_row_matches(item_name, row):
    """Exact name/path relationship only; never guess from similar names."""
    if not isinstance(row, dict):
        return False
    if normalize_queue_path(row.get("name", "")) == item_name:
        return True
    files = row.get("files") or []
    for entry in files:
        candidate = normalize_queue_path(entry.get("path", "") if isinstance(entry, dict) else "")
        if not candidate:
            continue
        if candidate == item_name or candidate.startswith(item_name + "/"):
            return True
    return False


def match_queue_row(item_name, queue_jobs):
    """Bind a /downloads item to its evidenced /queue row, or None.

    None means the queue cannot explain this item: the caller must fall back
    to the strict RPC path match (which itself yields — when unevidenced).
    Multiple live matches are abnormal: do not choose arbitrarily. Multiple
    offline stopped rows that agree (all telemetry_available false) share one
    deterministic representative (sorted by job_id); their state is identical
    (stopped with — values) and only the explanatory error text can differ.
    """
    if not queue_jobs:
        return None
    matches = [row for row in queue_jobs if queue_row_matches(item_name, row)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        return None
    live = [row for row in matches if row.get("telemetry_available")]
    if len(live) == 1:
        # Live evidence beats offline launch records for the same exact name.
        return live[0]
    if live:
        # Several live rows claim the same item: ambiguous, claim nothing.
        return None
    ordered = sorted(matches, key=lambda row: str(row.get("job_id", "")))
    return ordered[0]


def queue_state(item_name, row):
    """Map an evidenced /queue row to the table's STATE/%/DL/PEERS/HEALTH/TIME shape.

    Never invents telemetry: telemetry_available false always yields —
    values, and stopped never claims complete.
    """
    status = row.get("status")
    telem = bool(row.get("telemetry_available"))
    progress = row.get("progress") or {}
    percent = progress.get("percent")
    pct = round(float(percent), 1) if isinstance(percent, (int, float)) else None
    speed = row.get("download_speed")
    speed = int(speed) if isinstance(speed, (int, float)) else None
    peers = row.get("connections")
    peers = int(peers) if isinstance(peers, (int, float)) else None
    error = row.get("error")
    started_at = row.get("started_at")
    started = None
    if isinstance(started_at, (int, float)) and started_at > 0:
        try:
            started = f"started {stamp(started_at)}"
        except (OSError, OverflowError, ValueError):
            started = None

    def dl_text(value):
        if value is None:
            return "—"
        if value > 0:
            return f"{value / 1048576:.2f} MiB/s"
        return "idle"

    if status == "downloading" and telem:
        return {
            "state": "downloading",
            "pct": pct,
            "dl": dl_text(speed),
            "peers": peers,
            "health": "receiving" if (speed or 0) > 0 else "waiting",
            "time": started or ("receiving" if (speed or 0) > 0 else "awaiting data"),
            "source": "queue",
            "queue_status": status,
            "queue_job_id": row.get("job_id"),
        }
    if status == "downloading":
        return {
            "state": "waiting",
            "pct": None, "dl": "—", "peers": None,
            "health": "unknown",
            "time": str(error)[:64] if error else "—",
            "source": "queue",
            "queue_status": status,
            "queue_job_id": row.get("job_id"),
        }
    if status == "seeding":
        return {
            "state": "complete",
            "pct": pct if pct is not None else (100.0 if telem else None),
            "dl": "—",
            "peers": peers,
            "health": "seeding" if telem else ("error" if error else "stopped"),
            "time": "seeding" if telem else (str(error)[:64] if error else "stopped"),
            "source": "queue",
            "queue_status": status,
            "queue_job_id": row.get("job_id"),
        }
    if status == "completed":
        return {
            "state": "complete",
            "pct": pct if pct is not None else (100.0 if telem else None),
            "dl": "—",
            "peers": peers,
            "health": "complete",
            "time": "completed",
            "source": "queue",
            "queue_status": status,
            "queue_job_id": row.get("job_id"),
        }
    # stopped (or any unknown status): surface the error, never claim complete.
    if telem:
        return {
            "state": "stopped",
            "pct": pct,
            "dl": dl_text(speed),
            "peers": peers,
            "health": "error" if error else "stopped",
            "time": str(error)[:64] if error else "stopped",
            "source": "queue",
            "queue_status": status,
            "queue_job_id": row.get("job_id"),
        }
    return {
        "state": "stopped",
        "pct": None, "dl": "—", "peers": None,
        "health": "error" if error else "stopped",
        "time": str(error)[:64] if error else "stopped",
        "source": "queue",
        "queue_status": status,
        "queue_job_id": row.get("job_id"),
    }


def torrent_state(path, job, records, observations, now):
    controlled = has_control(path)
    if not job:
        # A control file proves it has not completed, but without an exact RPC
        # relation no progress/peer/time claim is trustworthy.
        return {
            "state": "waiting" if controlled else "complete",
            "pct": None, "dl": "—", "peers": None,
            "health": "unknown" if controlled else "complete",
            "time": "—" if controlled else "completed",
        }

    observation = update_observation(job, records, observations, now) or {}
    total = int(job.get("totalLength", 0) or 0)
    completed = int(job.get("completedLength", 0) or 0)
    speed = int(job.get("downloadSpeed", 0) or 0)
    peers = int(job.get("connections", 0) or 0)
    last_data = observation.get("last_data_at")
    started_at = observation.get("started_at")

    if speed > 0:
        health, time_value = "receiving", f"started {stamp(started_at)}" if started_at else "receiving"
    elif peers == 0:
        health, time_value = "starved", f"last data {age(last_data)}" if last_data else "awaiting peers"
    elif last_data:
        quiet_for = now - last_data
        health = "idle" if quiet_for < 300 else "stalled"
        time_value = f"last data {age(last_data)}"
    else:
        health, time_value = "waiting", f"started {stamp(started_at)}" if started_at else "awaiting data"

    return {
        "state": "downloading",
        "pct": round(completed / total * 100, 1) if total else None,
        "dl": f"{speed / 1048576:.2f} MiB/s" if speed else "idle",
        "peers": peers,
        "health": health,
        "time": time_value,
        "source": "rpc",
    }


def collect(queue_jobs="__live__", rpc_jobs="__live__"):
    # Default ("__live__") preserves the live behaviour: fetch /queue once, then
    # fall back to strict RPC matching only for rows /queue cannot explain.
    # Tests pass explicit values (including None for queue fetch failure).
    now = time.time()
    out = {"vpn": None, "jackett": "unknown", "items": []}
    status = curl_json("/status")
    if "_error" in status:
        out["vpn"] = {"state": "DOWN", "error": status["_error"]}
    else:
        host = re.search(r"Hostname:\s*(\S+)", status.get("raw", ""))
        uptime = re.search(r"Uptime:\s*([^\n]+)", status.get("raw", ""))
        out["vpn"] = {"state": "connected", "country": status.get("country", "?"),
                      "host": host.group(1) if host else "?",
                      "uptime": uptime.group(1).strip() if uptime else "?"}
        out["jackett"] = "up" if status.get("jackett_running") else "DOWN"

    records = load_records()
    observations = read_observations()
    queue_value = load_queue() if queue_jobs == "__live__" else queue_jobs
    jobs = rpc_active_jobs() if rpc_jobs == "__live__" else rpc_jobs
    for path in sorted(DOWNLOADS.iterdir()):
        if path.name.startswith(".") or path.suffix in {".aria2", ".torrent"}:
            continue
        row = match_queue_row(path.name, queue_value) if queue_value is not None else None
        if row is not None:
            data = queue_state(path.name, row)
        else:
            matching = [job for job in jobs if path_matches_job(path, job)]
            # Multiple exact matches are abnormal: do not choose one arbitrarily.
            job = matching[0] if len(matching) == 1 else None
            data = torrent_state(path, job, records, observations, now)
        out["items"].append({"name": path.name, "size": size(path), **data})
    write_observations(observations)
    return out


def render(data):
    lines = []
    vpn = data.get("vpn") or {}
    if vpn.get("state") == "connected":
        lines.append(f"VPN: {vpn['country']} — {vpn['host']} — up {vpn['uptime']}")
    else:
        lines.append(f"VPN: {vpn}")
    lines.append(f"Jackett: {data['jackett']}   now: {datetime.now().strftime('%H:%M')}")
    lines.append("")
    lines.append("%-38s %-11s %5s %9s %11s %7s %-11s %-18s" %
                 ("ITEM", "STATE", "%", "SIZE", "DL", "PEERS", "HEALTH", "TIME"))
    lines.append("─" * 124)
    for item in data["items"]:
        percentage = "—" if item["pct"] is None else str(item["pct"])
        peers = "—" if item["peers"] is None else str(item["peers"])
        lines.append("%-38s %-11s %5s %9s %11s %7s %-11s %-18s" %
                     (item["name"][:36], item["state"], percentage, human(item["size"]),
                      item["dl"], peers, item["health"], item["time"][:18]))
    lines.append("")
    lines.append("HEALTH: receiving = fresh aria2 bytes; idle/stalled = elapsed since last observed payload data; starved = 0 peers.")
    lines.append("TIME is evidence-based. Legacy jobs without an exact payload/RPC link deliberately show —.")
    lines.append("QUEUE: per-item %/DL/PEERS prefer exact GET /queue evidence; strict RPC path match is fallback only; — means no evidence.")
    return "\n".join(lines)


def run_selftest():
    """Offline checks with mocked /queue JSON (no network, stdlib only)."""
    failures = []

    def check(label, condition, detail=""):
        print(("PASS " if condition else "FAIL ") + label + (f" — {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(label)

    pressure = {
        "job_id": "9e2e87620da34f1f8987782109d2f5dc",
        "name": "Pressure (2026) [1080p] [WEBRip] [x265] [10bit] [5.1] [YTS.GG - YTS.BZ]",
        "status": "downloading",
        "progress": {"downloaded": 260795206, "total": 1806445382, "percent": 14.436927271571392},
        "download_speed": 970619,
        "upload_speed": 0,
        "connections": 44,
        "error": None,
        "telemetry_available": True,
        "files": [{"path": "Pressure (2026) [1080p] [WEBRip] [x265] [10bit] [5.1] [YTS.GG - YTS.BZ]/Pressure.2026.1080p.WEBRip.x265.10bit.AAC5.1-[YTS.GG - YTS.BZ].mp4",
                   "length": 1806110798, "completed": 254382158}],
    }
    stopped = {
        "job_id": "deadbeef",
        "name": "Old Show S01E01 720p AMZN WEBRip",
        "status": "stopped",
        "progress": {"downloaded": None, "total": None, "percent": None},
        "download_speed": None,
        "upload_speed": None,
        "connections": None,
        "error": "Telemetry unavailable: tracked aria2 process is no longer running",
        "telemetry_available": False,
        "files": [],
    }
    done = {
        "job_id": "complete1",
        "name": "Done Film (2023) 1080p WEB-DL",
        "status": "completed",
        "progress": {"downloaded": 1500000000, "total": 1500000000, "percent": 100.0},
        "download_speed": 0,
        "upload_speed": 12345,
        "connections": 3,
        "error": None,
        "telemetry_available": True,
        "files": [{"path": "Done Film (2023) 1080p WEB-DL/Done.Film.2023.mkv", "length": 1500000000, "completed": 1500000000}],
    }
    queue = [pressure, stopped, done]

    # 1. exact match binds telemetry.
    row = match_queue_row("Pressure (2026) [1080p] [WEBRip] [x265] [10bit] [5.1] [YTS.GG - YTS.BZ]", queue)
    check("exact match binds", row is not None and row.get("job_id") == pressure["job_id"])
    state = queue_state("x", row) if row else {}
    check("downloading speed>0 is receiving",
          state.get("health") == "receiving" and state.get("state") == "downloading",
          repr(state))
    check("downloading pct from queue", state.get("pct") == 14.4, repr(state.get("pct")))
    check("downloading dl from queue", state.get("dl") == f"{970619 / 1048576:.2f} MiB/s", repr(state.get("dl")))
    check("downloading peers from queue", state.get("peers") == 44, repr(state.get("peers")))

    # 2. no match stays unevidenced (caller falls back; queue claims nothing).
    check("no match yields None", match_queue_row("Unrelated Film (2024) 1080p", queue) is None)
    check("similar name is not a match",
          match_queue_row("Pressure (2026)", queue) is None,
          "prefix must not bind")

    # 3. stopped-with-error: stopped, — values, never complete.
    row = match_queue_row("Old Show S01E01 720p AMZN WEBRip", queue)
    state = queue_state("x", row) if row else {}
    check("stopped binds", row is not None)
    check("stopped state", state.get("state") == "stopped", repr(state))
    check("stopped keeps dash telemetry",
          state.get("pct") is None and state.get("dl") == "—" and state.get("peers") is None,
          repr(state))
    check("stopped surfaces error", state.get("health") == "error", repr(state))
    check("stopped does not claim complete",
          state.get("state") != "complete" and state.get("health") != "complete",
          repr(state))

    # 4. completed row.
    row = match_queue_row("Done Film (2023) 1080p WEB-DL", queue)
    state = queue_state("x", row) if row else {}
    check("completed binds", row is not None)
    check("completed maps to complete",
          state.get("state") == "complete" and state.get("health") == "complete",
          repr(state))

    # 5. /downloads prefix is still an exact match after normalization.
    prefixed = dict(stopped, name="/downloads/Old Show S01E01 720p AMZN WEBRip")
    check("downloads prefix normalizes",
          match_queue_row("Old Show S01E01 720p AMZN WEBRip", [prefixed]) is not None)

    # 6. file-path match for directory items.
    by_path = dict(pressure, name="something else entirely")
    check("payload path binds dir",
          match_queue_row("Pressure (2026) [1080p] [WEBRip] [x265] [10bit] [5.1] [YTS.GG - YTS.BZ]", [by_path]) is not None)

    print(f"selftest: {len(failures)} failure(s)")
    return 1 if failures else 0


def main():
    if "--selftest" in sys.argv:
        raise SystemExit(run_selftest())
    if "--json" in sys.argv:
        print(json.dumps(collect(), indent=2))
        return
    if "--watch" in sys.argv:
        while True:
            subprocess.run(["clear"], check=False)
            print(render(collect()))
            print("\nrefreshing every 10s — Ctrl-C to stop")
            time.sleep(10)
    print(render(collect()))


if __name__ == "__main__":
    main()
