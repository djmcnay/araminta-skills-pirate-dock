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


RPC_SECRET = "piratedockrpc"

def aria2_rpc_tellactive():
    """Query aria2 RPC ports 6800-6849 inside the container (ports unpublished)."""
    probe = """import json, urllib.request
jobs = []
for port in range(6800, 6810):
    try:
        body = json.dumps({'jsonrpc': '2.0', 'id': 's', 'method': 'aria2.tellActive',
            'params': ['token:piratedockrpc']}).encode()
        req = urllib.request.Request(f'http://127.0.0.1:{port}/jsonrpc', data=body,
            headers={'Content-Type': 'application/json'}, method='POST')
        d = json.loads(urllib.request.urlopen(req, timeout=1).read())
        for j in d.get('result', []):
            j['_port'] = port
            jobs.append(j)
    except Exception:
        pass
print(json.dumps(jobs))"""
    r = subprocess.run(["docker", "exec", "pirate-dock", "python3", "-c", probe],
                       capture_output=True, text=True, timeout=30)
    try:
        return json.loads(r.stdout)
    except Exception:
        return []


def aria2_pids():
    r = subprocess.run(["docker", "exec", "pirate-dock", "sh", "-c",
                        "ps -o pid=,stat= -C aria2c 2>/dev/null || pgrep -a aria2c"],
                       capture_output=True, text=True, timeout=15)
    out = []
    for ln in r.stdout.splitlines():
        parts = ln.split()
        if len(parts) >= 2 and parts[0].isdigit() and "Z" not in parts[1]:
            out.append(int(parts[0]))
    return out


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
    jobs = aria2_rpc_tellactive()
    out["aria2"]["rpc_jobs"] = jobs

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
        # start-time: earliest job-log touched after this dir appeared
        started = None
        for lf in sorted(DOWNLOADS.glob(".aria2-*.log")):
            if time.time() - lf.stat().st_mtime < 86400 * 7:
                st_ = lf.stat()
                started = datetime.fromtimestamp(st_.st_ctime).strftime("%H:%M %d")
                break
        item["started"] = started or "—"
        # live RPC job data (authoritative: %, speed, peers, seeds)
        # nameless jobs (resumed control files) get matched to unclaimed
        # downloading items AFTER named ones — handled in a second pass below.
        for j in out["aria2"].get("rpc_jobs", []):
            jname = j.get("bittorrent", {}).get("info", {}).get("name", "") or ""
            jd = j.get("dir", "")
            _norm = lambda s: re.sub(r"[._]", " ", s).lower().strip()
            _tok = lambda s: {t for t in re.split(r"\W+", _norm(s))
                              if t and t not in ("the", "a", "x264", "x265", "hevc",
                                                 "mkv", "mp4", "webrip", "bluray", "brrip")}
            if jname and (_norm(jname) in _norm(p.name) or _norm(p.name) in _norm(jname)
                          or jd.endswith(p.name)
                          or len(_tok(jname) & _tok(p.name)) >= 3):
                tc = int(j.get("totalLength", 0))
                cc = int(j.get("completedLength", 0))
                dl = int(j.get("downloadSpeed", 0))
                item.update({
                    "pct": round(cc / tc * 100, 1) if tc else None,
                    "dl_bps": dl,
                    "dl": "%.2f MiB/s" % (dl / 1048576) if dl else "idle",
                    "done": human(cc) if tc else None,
                    "total_est": human(tc) if tc else None,
                    "peers": int(j.get("connections", 0)),
                    "seeds": int(j.get("numSeeds", 0)) if j.get("numSeeds", "") != "" else None,
                    "leechers": max(0, int(j.get("connections", 0)) - int(j.get("numSeeds", 0) or 0)),
                    "eta": j.get("estimatedTime", ""),
                })
                break
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
    # second pass: assign nameless RPC jobs to unclaimed downloading items
    if out["aria2"].get("rpc_jobs"):
        unclaimed = [it for it in out["items"]
                     if it.get("state") == "downloading" and "pct" not in it]
        nameless = [j for j in out["aria2"]["rpc_jobs"]
                    if not j.get("bittorrent", {}).get("info", {}).get("name")]
        for j in nameless:
            if not unclaimed:
                break
            it = unclaimed.pop(0)
            tc = int(j.get("totalLength", 0))
            cc = int(j.get("completedLength", 0))
            dl = int(j.get("downloadSpeed", 0))
            it.update({
                "pct": round(cc / tc * 100, 1) if tc else None,
                "dl_bps": dl,
                "dl": "%.2f MiB/s" % (dl / 1048576) if dl else "idle",
                "peers": int(j.get("connections", 0)),
                "leechers": max(0, int(j.get("connections", 0)) - int(j.get("numSeeds", 0) or 0)),
            })
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
        lines.append("aria2: running (pids %s)" % ", ".join(str(x) for x in a["pids"]))
    else:
        lines.append("aria2: idle")
    lines.append("")
    lines.append("%-38s %-11s %5s %9s %11s %6s %8s %11s" % (
        "ITEM", "STATE", "%", "SIZE", "DL", "SEEDS", "LEECH", "STARTED"))
    lines.append("─" * 108)
    for it in d["items"]:
        started = it.get("started", "—")
        lines.append("%-38s %-11s %5s %9s %11s %6s %8s %11s" % (
            it["name"][:36], it.get("state", "?")[:11],
            str(it.get("pct", "—")) if it.get("pct") is not None else "—",
            human(it.get("size", 0)),
            it.get("dl", "—"), str(it.get("seeds", "—")) if it.get("seeds") is not None else "—",
            str(it.get("leechers", "—")) if it.get("leechers") is not None else "—",
            str(started)[:11]))
    lines.append("")
    lines.append("(complete items: — fields; live items get full data from aria2 RPC)")
    return chr(10).join(lines)



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