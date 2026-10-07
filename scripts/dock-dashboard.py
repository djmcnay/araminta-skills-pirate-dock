#!/usr/bin/env python3
"""
pirate-dock Gradio Dashboard
============================
A web UI for monitoring and controlling the pirate-dock from outside the container.

Features:
- VPN status (connected server, country, IP, uptime) + 1-button rotation
- Download queue table (live, auto-refreshing every 15s)
- Health check — full output from dock-health.py

Runs on the Pi. Accessible via Tailscale at http://100.65.212.67:7860

Usage:
    python3 dock-dashboard.py [--host HOST] [--port PORT]

Requirements:
    pip install gradio requests
"""

import argparse
import json
import os
import subprocess
import time
import urllib.request
import urllib.parse

import gradio as gr

# ─── Configuration ───────────────────────────────────────────────────────────

RPC_PORT = int(os.environ.get("ARIA2_RPC_PORT", "6800"))
RPC_SECRET = os.environ.get("ARIA2_RPC_SECRET", "piratedockrpc")

VPN_COUNTRY_CODE = os.environ.get("VPN_COUNTRY_CODE", "uk")

REFRESH_INTERVAL = 15


# ─── Helpers ─────────────────────────────────────────────────────────────────

def docker_exec(cmd: list, timeout: int = 15) -> tuple:
    """Run a command inside the pirate-dock container."""
    full_cmd = ["docker", "exec", "pirate-dock"] + cmd
    result = subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def format_size(size_bytes: int) -> str:
    """Format bytes to human readable."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 ** 3:
        return f"{size_bytes / 1024 ** 2:.1f} MB"
    else:
        return f"{size_bytes / 1024 ** 3:.2f} GB"


def parse_size(size_str: str) -> int:
    """Parse a formatted size string back to bytes for summation."""
    if size_str == "—":
        return 0
    try:
        parts = size_str.split()
        if len(parts) != 2:
            return 0
        val = float(parts[0])
        unit = parts[1].replace("/s", "")
        multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}
        return int(val * multipliers.get(unit, 1))
    except:
        return 0


# ─── VPN Helpers ─────────────────────────────────────────────────────────────

COUNTRY_FLAGS = {
    "united kingdom": "🇬🇧", "uk": "🇬🇧", "great britain": "🇬🇧",
    "south africa": "🇿🇦", "sa": "🇿🇦",
    "united states": "🇺🇸", "usa": "🇺🇸", "us": "🇺🇸",
    "netherlands": "🇳🇱", "germany": "🇩🇪", "france": "🇫🇷",
    "canada": "🇨🇦", "australia": "🇦🇺", "japan": "🇯🇵",
    "sweden": "🇸🇪", "switzerland": "🇨🇭", "singapore": "🇸🇬",
    "ireland": "🇮🇪", "spain": "🇪🇸", "italy": "🇮🇹",
    "brazil": "🇧🇷", "india": "🇮🇳", "poland": "🇵🇱",
    "norway": "🇳🇴", "denmark": "🇩🇰", "finland": "🇫🇮",
    "belgium": "🇧🇪", "austria": "🇦🇹", "portugal": "🇵🇹",
    "mexico": "🇲🇽", "argentina": "🇦🇷", "russia": "🇷🇺",
    "ukraine": "🇺🇦", "turkey": "🇹🇷", "israel": "🇮🇱",
    "new zealand": "🇳🇿", "czech republic": "🇨🇿", "romania": "🇷🇴",
    "hungary": "🇭🇺", "greece": "🇬🇷", "iceland": "🇮🇸",
}


def country_flag(country_name: str) -> str:
    """Get flag emoji for a country name."""
    if not country_name:
        return ""
    return COUNTRY_FLAGS.get(country_name.lower().strip(), "🌐")


def format_uptime(uptime_str: str) -> str:
    """Format '5 minutes 23 seconds' → '5:23' (digital clock style)."""
    if not uptime_str:
        return "—"
    import re
    # Match patterns like "5 minutes 23 seconds", "1 hour 5 minutes", "45 seconds"
    hours = 0
    minutes = 0
    seconds = 0
    h_match = re.search(r"(\d+)\s*hour", uptime_str, re.IGNORECASE)
    m_match = re.search(r"(\d+)\s*min", uptime_str, re.IGNORECASE)
    s_match = re.search(r"(\d+)\s*sec", uptime_str, re.IGNORECASE)
    if h_match:
        hours = int(h_match.group(1))
    if m_match:
        minutes = int(m_match.group(1))
    if s_match:
        seconds = int(s_match.group(1))
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    else:
        return f"{minutes}:{seconds:02d}"


# ─── VPN ─────────────────────────────────────────────────────────────────────

def get_vpn_status() -> dict:
    """Get current VPN connection status (runs inside container)."""
    try:
        out, err, rc = docker_exec(["nordvpn", "status"])
        if rc != 0:
            return {"connected": False, "error": err.strip() or "nordvpn not available in container"}

        status = {"connected": False, "protocol": "", "country": "", "city": "",
                  "ip": "", "uptime": "", "raw": out}

        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Status:"):
                status["connected"] = "Connected" in line
            elif line.startswith("Current protocol:"):
                status["protocol"] = line.split(":", 1)[1].strip() if ":" in line else ""
            elif line.startswith("Country:"):
                status["country"] = line.split(":", 1)[1].strip() if ":" in line else ""
            elif line.startswith("City:"):
                status["city"] = line.split(":", 1)[1].strip() if ":" in line else ""
            elif line.startswith("IP:"):
                status["ip"] = line.split(":", 1)[1].strip() if ":" in line else ""
            elif line.startswith("Hostname:"):
                status["current_server"] = line.split(":", 1)[1].strip() if ":" in line else ""
            elif line.startswith("Uptime:"):
                status["uptime"] = line.split(":", 1)[1].strip() if ":" in line else ""

        return status
    except subprocess.TimeoutExpired:
        return {"connected": False, "error": "nordvpn status timed out"}
    except Exception as e:
        return {"connected": False, "error": str(e)}


def rotate_vpn() -> dict:
    """Rotate VPN to a new server in the configured country."""
    try:
        docker_exec(["nordvpn", "disconnect"])
        time.sleep(2)
        out, err, rc = docker_exec(["nordvpn", "connect", VPN_COUNTRY_CODE], timeout=30)
        if rc != 0:
            return {"success": False, "error": err.strip() or out.strip() or "Connection failed"}
        time.sleep(3)
        status = get_vpn_status()
        status["success"] = status["connected"]
        return status
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "VPN rotation timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ─── Download Queue ──────────────────────────────────────────────────────────

def get_rpc_secret() -> str:
    """Get the aria2 RPC secret."""
    return RPC_SECRET


def rpc_call(method: str, params: list = None) -> dict:
    """Make an aria2 RPC call via docker exec (runs inside container)."""
    secret = get_rpc_secret()

    # Build params array: prepend token if secret exists
    rpc_params = []
    if secret:
        rpc_params.append(f"token:{secret}")
    if params:
        rpc_params.extend(params)

    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": "dashboard",
        "method": method,
        "params": rpc_params
    })

    cmd = [
        "docker", "exec", "pirate-dock",
        "curl", "-s", "-X", "POST",
        "http://127.0.0.1:6800/jsonrpc",
        "-H", "Content-Type: application/json",
        "-d", payload
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return {"error": result.stderr.strip() or f"curl exit {result.returncode}"}
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {str(e)}"}
    except Exception as e:
        return {"error": str(e)}


def get_download_queue() -> list:
    """Get the current download queue from aria2."""
    active = rpc_call("aria2.tellActive")
    waiting = rpc_call("aria2.tellWaiting", [0, 100])
    stopped = rpc_call("aria2.tellStopped", [0, 20])

    items = []
    for job in active.get("result", []):
        items.append(format_job(job, "active"))
    for job in waiting.get("result", []):
        items.append(format_job(job, "waiting"))
    for job in stopped.get("result", []):
        items.append(format_job(job, "stopped"))
    return items


def format_job(job: dict, status: str) -> dict:
    """Format an aria2 job for display."""
    info = job.get("bittorrent", {}).get("info", {})
    files = job.get("files", [])
    name = info.get("name", files[0].get("path", "?") if files else "?")
    name = name.replace("[EZTVx.to]", "").replace("[YTS.MX]", "").replace("[YTS]", "").strip()

    total = int(job.get("totalLength", 0))
    completed = int(job.get("completedLength", 0))
    speed = int(job.get("downloadSpeed", 0))
    upload_speed = int(job.get("uploadSpeed", 0))
    connections = job.get("connections", 0)
    num_pieces = job.get("numPieces", 0)
    completed_pieces = job.get("completedPieces", 0)

    pct = (completed / total * 100) if total > 0 else 0

    total_str = format_size(total)
    completed_str = format_size(completed)
    speed_str = format_size(speed) + "/s" if speed > 0 else "—"
    upload_str = format_size(upload_speed) + "/s" if upload_speed > 0 else "—"

    if status == "active":
        icon = "🟢"
    elif status == "waiting":
        icon = "🟡"
    else:
        icon = "⚪"

    return {
        "Status": icon,
        "Name": name[:80],
        "Progress": f"{pct:.1f}%",
        "Size": total_str,
        "Downloaded": completed_str,
        "DL Speed": speed_str,
        "UL Speed": upload_str,
        "Peers": str(connections),
        "Pieces": f"{completed_pieces}/{num_pieces}",
    }


# ─── Health Check ────────────────────────────────────────────────────────────

def get_health_output() -> str:
    """Run dock-health.py and return full output."""
    try:
        result = subprocess.run(
            ["python3", os.path.expanduser("~/Documents/GitHub/pirate-dock/scripts/dock-health.py")],
            capture_output=True, text=True, timeout=30
        )
        output = result.stdout
        if result.stderr:
            output += "\n" + result.stderr
        return output.strip()
    except Exception as e:
        return f"Health check failed: {e}"


# ─── Gradio Interface ────────────────────────────────────────────────────────

def create_dashboard():
    """Create the Gradio dashboard."""

    with gr.Blocks(title="pirate-dock") as demo:
        gr.Markdown("# 🏴‍☠️ pirate-dock")

        # ─── VPN Section ───
        with gr.Row():
            gr.Markdown("## VPN Status")
            with gr.Column():
                with gr.Row():
                    vpn_refresh = gr.Button("Refresh Status", variant="secondary")
                    vpn_rotate = gr.Button("Rotate VPN", variant="primary")

        with gr.Row():
            vpn_traffic_light = gr.Textbox(label="Status", interactive=False)
            vpn_connected = gr.Textbox(label="Connection", interactive=False)
            vpn_country = gr.Textbox(label="Country", interactive=False)
            vpn_city = gr.Textbox(label="City", interactive=False)
            vpn_uptime = gr.Textbox(label="Uptime", interactive=False)
            vpn_server = gr.Textbox(label="Server", interactive=False)
            vpn_ip = gr.Textbox(label="IP", interactive=False)
            vpn_protocol = gr.Textbox(label="Protocol", interactive=False)

        # ─── Stats Section ───
        gr.Markdown("## Queue Summary")
        with gr.Row():
            stats_active = gr.Textbox(label="Active", interactive=False)
            stats_waiting = gr.Textbox(label="Waiting", interactive=False)
            stats_stopped = gr.Textbox(label="Completed", interactive=False)
            stats_dl = gr.Textbox(label="DL Speed", interactive=False)
            stats_ul = gr.Textbox(label="UL Speed", interactive=False)

        # ─── Queue Section ───
        gr.Markdown("## Download Queue")
        queue_table = gr.DataFrame(
            headers=["Status", "Name", "Progress", "Size", "Downloaded", "DL Speed", "UL Speed", "Peers", "Pieces"],
            datatype=["str", "str", "str", "str", "str", "str", "str", "str", "str"],
            interactive=False,
            wrap=True,
        )
        queue_refresh = gr.Button("Refresh Queue", variant="secondary")

        # ─── Health Section ───
        gr.Markdown("## Health Check")
        health_output = gr.Textbox(
            label="dock-health.py output",
            lines=20,
            interactive=False,
        )
        health_refresh = gr.Button("Run Health Check", variant="secondary")

        # ─── Data loading functions ───
        def load_vpn():
            s = get_vpn_status()
            if s.get("connected"):
                light = "🟢"
                country = f"{country_flag(s.get('country', ''))} {s.get('country', '?')}"
                uptime = format_uptime(s.get("uptime", ""))
                return (light, "Connected", country, s.get("city", "?"),
                        uptime, s.get("current_server", "?"), s.get("ip", "?"),
                        s.get("protocol", "?"))
            else:
                light = "🔴"
                return (light, f"Disconnected ({s.get('error', 'unknown')})",
                        "", "", "", "", "", "")

        def load_stats():
            items = get_download_queue()
            active = sum(1 for i in items if i["Status"] == "🟢")
            waiting = sum(1 for i in items if i["Status"] == "🟡")
            stopped = sum(1 for i in items if i["Status"] == "⚪")
            total_dl = sum(parse_size(i["DL Speed"]) for i in items if i["Status"] == "🟢")
            total_ul = sum(parse_size(i["UL Speed"]) for i in items if i["Status"] == "🟢")
            return (str(active), str(waiting), str(stopped),
                    f"{format_size(total_dl)}/s", f"{format_size(total_ul)}/s")

        def load_queue():
            items = get_download_queue()
            if not items:
                return [["—", "No downloads in queue", "—", "—", "—", "—", "—", "—", "—"]]
            return [[i["Status"], i["Name"], i["Progress"], i["Size"], i["Downloaded"],
                     i["DL Speed"], i["UL Speed"], i["Peers"], i["Pieces"]] for i in items]

        def load_health():
            return get_health_output()

        def do_rotate():
            result = rotate_vpn()
            return load_vpn()

        # Wire up events
        vpn_outputs = [vpn_traffic_light, vpn_connected, vpn_country, vpn_city,
                       vpn_uptime, vpn_server, vpn_ip, vpn_protocol]
        vpn_refresh.click(load_vpn, outputs=vpn_outputs)
        vpn_rotate.click(do_rotate, outputs=vpn_outputs)
        queue_refresh.click(load_queue, outputs=[queue_table])
        health_refresh.click(load_health, outputs=[health_output])

        # Auto-refresh
        timer = gr.Timer(REFRESH_INTERVAL)
        timer.tick(load_vpn, outputs=vpn_outputs)
        timer.tick(load_queue, outputs=[queue_table])
        timer.tick(load_stats, outputs=[stats_active, stats_waiting, stats_stopped, stats_dl, stats_ul])

    return demo


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="pirate-dock Gradio Dashboard")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=7860, help="Port to bind to")
    parser.add_argument("--share", action="store_true", help="Create a Gradio share link")
    args = parser.parse_args()

    demo = create_dashboard()
    demo.launch(
        server_name=args.host,
        server_port=args.port,
        share=args.share,
        show_error=True,
    )


if __name__ == "__main__":
    main()
