#!/usr/bin/env python3
"""pirate-dock status: torrents, sizes, speeds, peers, VPN — one table.

On the Pi:  python3 ~/Documents/GitHub/pirate-dock/scripts/dock-status.py
Options:    --watch   refresh every 10s
            --json    machine-readable
From Mac:   ssh araminta "python3 ~/Documents/GitHub/pirate-dock/scripts/dock-status.py"
"""
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import urllib.request

DOWNLOADS = Path("/home/djmcnay/Documents/GitHub/pirate-dock/downloads")
API = "http://localhost:9876"
VIDEO = {".mkv", ".mp4", ".avi", ".webm", ".m4v", ".ts"}


def curl_json(path, timeout=8):
    try:
        return json.loads(urllib.request.urlopen(f"{API}{path}", timeout=timeout).read())
    except Exception as e:
        return {"_error": str(e)}


def human(n):
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024:
            return f"{int(n)} B" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def age_str(t):
    d = time.time() - t
    if d < 120:
        return f"{int(d)}s ago"
    if d < 7200:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


def aria2_pids():
    r = subprocess.run(["docker", "exec", "pirate-dock", "pgrep", "aria2c"],
                       capture_output=True, text=True, timeout=15)
    return [int(x) for x in r.stdout.split() if x.isdigit()]


def proc_rchar(pid):
    try:
        r = subprocess.run(["docker", "exec", "pirate-dock", "cat", f"/proc/{pid}/io"],
                           capture_output=True, text=True, timeout=10)
        m = re.search(r"rchar:\s*(\d+)", r.stdout)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def aria2_summary_line(pid):
    """Progress line from per-job logs (written by server.py since 38fed92)."""
    for cand in ("/tmp/aria2-run.log", "/tmp/aria2.log", "/downloads/.aria2-latest.log"):
        r = subprocess.run(
            ["docker", "exec", "pirate-dock", "sh", "-c",
             "grep -E '^\\[#' %s 2>/dev/null | tail -1" % cand],
            capture_output=True, text=True, timeout=10)
        if r.stdout.strip():
            return r.stdout.strip()
    return None


def fd_writes_into(pid, name):
    r = subprocess.run(
        ["docker", "exec", "pirate-dock", "sh", "-c",
         "ls -la /proc/%d/fd 2>/dev/null | grep -F '%s' | head -1" % (pid, name)],
        capture_output=True, text=True, timeout=10)
    return bool(r.stdout.strip())


def collect():
    now = time.time()
    out = {"vpn": None, "jackett": None, "aria2": {"pids": [], "rate_bps": 0}, "items": []}

    st = curl_json("/status")
    if "_error" in st:
        out["vpn"] = {"state": "DOWN", "error": st["_error"]}
        out["jackett"] = "unknown"
    else:
        hm = re.search(r"Hostname:\s*(\S+)", st.get("raw", ""))
        up = re.search(r"Uptime:\s*([^\n]+)", st.get("raw", ""))
        out["vpn"] = {"state": "connected", "country": st.get("country", "?"),
                      "host": hm.group(1) if hm else "?",
                      "uptime": up.group(1).strip() if up else "?"}
        out["jackett"] = "up" if st.get("jackett_running") else "DOWN"

    pids = aria2_pids()
    out["aria2"]["pids"] = pids
    rates = {}
    for pid in pids:
        a = proc_rchar(pid)
        time.sleep(2)
        b = proc_rchar(pid)
        if a is not None and b is not None:
            rates[pid] = (b - a) / 2
    out["aria2"]["rate_bps"] = sum(rates.values())
    out["aria2"]["rates"] = rates

    listed = set()
    # jobs from per-job logs (picks up items whose files haven't landed yet)
    job_items = {}
    for lf in sorted(DOWNLOADS.glob(".aria2-*.log")):
        try:
            txt = lf.read_text(errors="ignore")
        except Exception:
            continue
        m = re.search(r"Download complete: \[MEMORY\]\[METADATA\](.+)", txt)
        if m:
            job_items[lf.name] = {"title_hint": m.group(1).strip()}
    for p in sorted(DOWNLOADS.iterdir()):
        if p.name.startswith(".") or p.suffix == ".torrent":
            continue
        if p.is_file():
            st_ = p.stat()
            out["items"].append({"name": p.name, "type": "file", "state": "complete",
                                 "size": st_.st_size, "activity": age_str(st_.st_mtime)})
            continue
        total, newest = 0, 0
        for f in p.rglob("*"):
            if f.is_file():
                s = f.stat()
                total += s.st_size
                newest = max(newest, s.st_mtime)
        # control signal may sit beside the dir OR beside its first file
        ctl = None
        cand = DOWNLOADS / (p.name + ".aria2")
        if cand.exists():
            ctl = cand
        else:
            for f in p.rglob("*.aria2"):
                ctl = f
                break
        item = {"name": p.name, "type": "dir", "size": total,
                "state": "downloading" if ctl else "complete",
                "activity": age_str(newest)}
        if item["state"] == "downloading" and pids:
            for pid in pids:
                r = subprocess.run(
                    ["docker", "exec", "pirate-dock", "sh", "-c",
                     "ls -la /proc/%d/fd 2>/dev/null | grep -F '%s' | head -1" % (pid, p.name)],
                    capture_output=True, text=True, timeout=10)
                if r.stdout.strip():
                    item["pid"] = pid
                    r_bps = rates.get(pid, 0)
                    item["dl_bps"] = r_bps
                    item["dl"] = "%.1f MiB/s" % (r_bps / 1048576) if r_bps else "idle"
                    # summary line from per-job logs
                    for lf in sorted(DOWNLOADS.glob(".aria2-*.log")):
                        try:
                            tail = lf.read_text(errors="ignore").strip().splitlines()[-1:]
                        except Exception:
                            continue
                        for ln in tail:
                            pct = re.search(r"\((\d+)%\)", ln)
                            done = re.search(r"\[#\w+\s+([\d.]+[KMG]?i?B)/([\d.]+[KMG]?i?B)", ln)
                            dl = re.search(r"DL:([\d.]+[KMG]?i?B)", ln)
                            ul = re.search(r"UL:([\d.]+[KMG]?i?B)", ln)
                            sd = re.search(r"(?:SD|SEED):(\d+)", ln)
                            cn = re.search(r"CN:(\d+)", ln)
                            if pct: item["pct"] = int(pct.group(1))
                            if done:
                                item["done"], item["total_est"] = done.group(1), done.group(2)
                            if dl: item["dl_from_log"] = dl.group(1)
                            if ul: item["ul_from_log"] = ul.group(1)
                            if sd: item["seeds"] = int(sd.group(1))
                            if cn: item["peers"] = int(cn.group(1))
                    break
        out["items"].append(item)
    return out


def render(d):
    lines = []
    vpn = d.get("vpn") or {}
    if vpn.get("state") == "connected":
        lines.append("VPN: %s — %s — up %s" % (vpn.get("country"), vpn.get("host"), vpn.get("uptime")))
    else:
        lines.append("VPN: %s" % (vpn or {"state": "unknown"}))
    lines.append("Jackett: %s   API: %s   now: %s" % (d.get("jackett"), API,
                                                      datetime.now().strftime("%H:%M")))
    a = d.get("aria2", {})
    if a["pids"]:
        msg = "aria2: running (pids %s)" % ", ".join(str(x) for x in a["pids"])
        if a["rate_bps"]:
            msg += "   total pull: %.2f MiB/s" % (a["rate_bps"] / 1048576)
        lines.append(msg)
    else:
        lines.append("aria2: idle")
    lines.append("")
    lines.append("%-42s %-12s %9s %4s %9s %7s %12s" % (
        "ITEM", "STATE", "SIZE", "%", "DL", "PEERS", "ACTIVITY"))
    lines.append("─" * 96)
    for it in d["items"]:
        lines.append("%-42s %-12s %9s %4s %9s %7s %12s" % (
            it["name"][:40], it.get("state", "?")[:12], human(it.get("size", 0)),
            str(it.get("pct", "—")), it.get("dl", "—"), str(it.get("peers", "—")),
            it.get("activity", "—")))
    lines.append("")
    lines.append("(idle items show no speeds; per-torrent DL/peers appear while downloading)")
    return "\n".join(lines)


def main():
    if "--json" in sys.argv:
        print(json.dumps(collect(), indent=2))
        return
    if "--watch" in sys.argv:
        while True:
            subprocess.run(["clear"])
            print(render(collect()))
            print("\nrefreshing every 10s — Ctrl-C to stop")
            time.sleep(10)
    print(render(collect()))


if __name__ == "__main__":
    main()