#!/usr/bin/env python3
"""Reliable, conservative torrent-status table for pirate-dock.

On the Pi:  python3 ~/Documents/GitHub/pirate-dock/scripts/dock-status.py
Options:    --watch   refresh every 10s
            --json    machine-readable
From Mac:   ssh araminta "python3 ~/Documents/GitHub/pirate-dock/scripts/dock-status.py"

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
    }


def collect():
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
    jobs = rpc_active_jobs()
    for path in sorted(DOWNLOADS.iterdir()):
        if path.name.startswith(".") or path.suffix in {".aria2", ".torrent"}:
            continue
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
    return "\n".join(lines)


def main():
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
