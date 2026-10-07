#!/usr/bin/env python3
"""
pirate-dock Gradio Dashboard
============================
A web UI for monitoring and controlling the pirate-dock from outside the container.

Features:
- VPN status (connected server, country, IP, uptime, transfer) + 1-button rotation
- Download queue table (live from GET /queue, auto-refreshing every 15s)
- Selected-download details panel with pause/resume/transfer/delete actions
- Health check — FULL dock-health.py output plus its vault report, scrollable

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
import urllib.error
from pathlib import Path

import gradio as gr

# ─── Configuration ───────────────────────────────────────────────────────────

VPN_COUNTRY_CODE = os.environ.get("VPN_COUNTRY_CODE", "uk")

QUEUE_API = os.environ.get("QUEUE_API", "http://localhost:9876")

HEALTH_REPORT = Path(
    os.path.expanduser("~/araminta-vault/99_system/health-check/pirate-dock.md")
)

REFRESH_INTERVAL = 15

EMPTY_SELECTION = "Select a download to see its name."


# ─── Helpers ─────────────────────────────────────────────────────────────────

def docker_exec(cmd: list, timeout: int = 15) -> tuple:
    """Run a command inside the pirate-dock container."""
    full_cmd = ["docker", "exec", "pirate-dock"] + cmd
    result = subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def format_size(size_bytes) -> str:
    """Format bytes to human readable. None renders as an em dash (unknown)."""
    if size_bytes is None:
        return "—"
    try:
        size_bytes = int(size_bytes)
    except (TypeError, ValueError):
        return "—"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 ** 3:
        return f"{size_bytes / 1024 ** 2:.1f} MB"
    else:
        return f"{size_bytes / 1024 ** 3:.2f} GB"


def clean_name(raw) -> str:
    """Strip launch-metadata prefixes so payload names display cleanly."""
    name = str(raw or "")
    for _ in range(4):
        if name.startswith("[METADATA]"):
            name = name[len("[METADATA]"):].strip()
        elif name.startswith("[MEMORY]"):
            name = name[len("[MEMORY]"):].strip()
        else:
            break
    return name or str(raw or "?")


# ─── VPN helpers ─────────────────────────────────────────────────────────────

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

        status = {"connected": False, "protocol": "", "technology": "",
                  "country": "", "city": "", "ip": "", "uptime": "",
                  "transfer": "", "raw": out}

        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Status:"):
                status["connected"] = "Connected" in line
            elif line.startswith("Current technology:"):
                status["technology"] = line.split(":", 1)[1].strip() if ":" in line else ""
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
            elif line.startswith("Transfer:"):
                status["transfer"] = line.split(":", 1)[1].strip() if ":" in line else ""
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

def get_download_queue() -> list:
    """Read the server's tracked queue, including jobs with no live aria2."""
    with urllib.request.urlopen(f"{QUEUE_API}/queue", timeout=15) as response:
        data = json.load(response)
    jobs = data.get("jobs", [])
    return jobs


def format_job(job: dict) -> dict:
    """Render null telemetry as unknown (em dash) rather than invented zeros.

    Takes a GET /queue job row (job_id, name, status, progress{downloaded,
    total, percent}, download_speed, upload_speed, connections, error, gid).
    NOTE: self-contained on purpose — the repo's queue regression test execs
    this function in isolation, so it must not call other module helpers.
    """
    progress = job.get("progress") or {}

    def size(value, speed=False):
        if value is None:
            return "—"
        return format_size(value) + ("/s" if speed else "")

    pct = progress.get("percent")
    try:
        progress_str = "—" if pct is None else f"{float(pct):.1f}%"
    except (TypeError, ValueError):
        progress_str = "—"

    raw_name = job.get("name") or job.get("job_id") or "?"
    name = str(raw_name)
    for _ in range(4):
        if name.startswith("[METADATA]"):
            name = name[len("[METADATA]"):].strip()
        elif name.startswith("[MEMORY]"):
            name = name[len("[MEMORY]"):].strip()
        else:
            break
    if not name:
        name = str(raw_name)

    return {
        "Status": job.get("status") or "stopped",
        "Name": name,
        "Progress": progress_str,
        "Size": size(progress.get("total")),
        "Downloaded": size(progress.get("downloaded")),
        "DL Speed": size(job.get("download_speed"), True),
        "UL Speed": size(job.get("upload_speed"), True),
        "Peers": "—" if job.get("connections") is None else str(job.get("connections")),
        "Error": job.get("error") or "",
        "Job ID": f"{job.get('job_id') or ''}:{job.get('gid') or ''}",
    }


def selected_download(evt) -> str:
    """Map a queue-table .select() event to the selected job's display name."""
    try:
        if evt is None or not getattr(evt, "selected", True):
            return EMPTY_SELECTION
        row = getattr(evt, "row_value", None)
        if isinstance(row, (list, tuple)) and len(row) > 1 and row[1]:
            return str(row[1])
        value = getattr(evt, "value", None)
        if value:
            return str(value)
    except Exception:
        pass
    return EMPTY_SELECTION


def detail_values(job: dict) -> tuple:
    """Full detail-panel values for one GET /queue job row.

    Returns (name, status_text, progress_slider_update, size_text,
    files_rows, pause_update, resume_update, transfer_update, delete_update).
    Unknown telemetry renders as em dashes / 'unknown' — never fake zeros —
    and all action buttons disable when the job is not actionable.
    """
    job = job or {}
    name = job.get("name") or EMPTY_SELECTION
    status = job.get("status") or "stopped"
    aria = str(job.get("aria2_status") or "").lower()
    progress = job.get("progress") or {}
    pct = progress.get("percent")
    downloaded = progress.get("downloaded")
    total = progress.get("total")
    err = job.get("error")

    try:
        pct_f = float(pct) if pct is not None else None
    except (TypeError, ValueError):
        pct_f = None

    if pct_f is None:
        status_text = f"{status} — progress unknown"
        if err:
            status_text += f" ({err})"
        slider = gr.update(value=0, visible=False)
    else:
        status_text = f"{status} — {pct_f:.1f}%"
        if aria:
            status_text += f" ({aria})"
        slider = gr.update(value=round(pct_f, 1), visible=True)

    size_text = f"{format_size(downloaded)} / {format_size(total)}"

    files_rows = []
    for entry in job.get("files") or []:
        files_rows.append([
            entry.get("path", "?"),
            format_size(entry.get("completed")),
            format_size(entry.get("length")),
        ])

    pause = gr.update(value="Pause", interactive=aria in ("active", "waiting"))
    resume = gr.update(value="Resume", interactive=aria == "paused")
    transfer = gr.update(value="Transfer", interactive=aria == "complete")
    delete = gr.update(value="Delete", interactive=aria in ("paused", "complete", "error"))

    return (name, status_text, slider, size_text, files_rows,
            pause, resume, transfer, delete)


# ─── Queue actions ───────────────────────────────────────────────────────────

def find_job(jobs: list, job_id: str, gid: str):
    """Find one queue row by job_id (+ gid when the row carries telemetry)."""
    for job in jobs:
        if job.get("job_id") != job_id:
            continue
        if gid and job.get("gid") and job.get("gid") != gid:
            continue
        return job
    return None


def lookup_selection(name: str):
    """Resolve a display name to (state, job). Prefers live telemetry rows."""
    try:
        jobs = get_download_queue()
    except Exception:
        return None, {}
    if not name or name == EMPTY_SELECTION:
        return None, {}
    candidates = [j for j in jobs if clean_name(j.get("name", "")) == name]
    if not candidates:
        return None, {}
    live = [j for j in candidates if j.get("telemetry_available")]
    job = live[0] if live else candidates[0]
    return {"job_id": job.get("job_id"), "gid": job.get("gid")}, job


def queue_action(job_id: str, gid: str, action: str, confirm: bool = False) -> tuple:
    """POST one queue action. Returns (ok, message) — errors are surfaced."""
    body = json.dumps({"gid": gid, "confirm": bool(confirm)}).encode()
    req = urllib.request.Request(
        f"{QUEUE_API}/queue/{job_id}/{action}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            try:
                data = json.loads(resp.read().decode() or "{}")
            except Exception:
                data = {}
        msg = data.get("message") or f"{action} accepted (status: {data.get('status', 'ok')})."
        if data.get("request_id"):
            msg += f" Request: {data['request_id']}."
        return True, msg
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("detail", "")
        except Exception:
            detail = getattr(e, "reason", e)
        return False, f"{action} rejected ({e.code}): {detail}"
    except Exception as e:
        return False, f"{action} failed: {e}"


# ─── Health Check ────────────────────────────────────────────────────────────

def get_health_output() -> str:
    """Run dock-health.py and return its FULL output plus the vault report."""
    try:
        result = subprocess.run(
            ["python3", os.path.expanduser("~/Documents/GitHub/pirate-dock/scripts/dock-health.py")],
            capture_output=True, text=True, timeout=60
        )
        output = result.stdout or ""
        if result.stderr:
            output += "\n" + result.stderr
        output = output.strip()
    except Exception as e:
        output = f"Health check failed: {e}"
    try:
        if HEALTH_REPORT.exists():
            report = HEALTH_REPORT.read_text().strip()
            if report:
                output += "\n\n———— full report: pirate-dock.md ————\n" + report
    except Exception as e:
        output += f"\n\n(Could not read vault report: {e})"
    return output.strip()


# ─── Gradio Interface ────────────────────────────────────────────────────────

QUEUE_HEADERS = ["Status", "Name", "Progress", "Size", "Downloaded",
                 "DL Speed", "UL Speed", "Peers", "Error", "Job ID"]
QUEUE_WIDTHS = ["110px", "280px", "90px", "90px", "110px",
                "110px", "110px", "70px", "220px", "160px"]


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
            vpn_technology = gr.Textbox(label="Technology", interactive=False)
            vpn_protocol = gr.Textbox(label="Protocol", interactive=False)
            vpn_transfer = gr.Textbox(label="Transfer", interactive=False)

        vpn_outputs = [vpn_traffic_light, vpn_connected, vpn_country, vpn_city,
                       vpn_uptime, vpn_server, vpn_ip, vpn_technology,
                       vpn_protocol, vpn_transfer]

        # ─── Stats Section ───
        gr.Markdown("## Queue Summary")
        with gr.Row():
            stats_active = gr.Textbox(label="Active", interactive=False)
            stats_stopped = gr.Textbox(label="Stopped", interactive=False)
            stats_completed = gr.Textbox(label="Completed", interactive=False)
            stats_dl = gr.Textbox(label="DL Speed", interactive=False)
            stats_ul = gr.Textbox(label="UL Speed", interactive=False)

        # ─── Queue Section ───
        gr.Markdown("## Download Queue")
        queue_table = gr.DataFrame(
            headers=QUEUE_HEADERS,
            datatype=["str"] * len(QUEUE_HEADERS),
            column_widths=QUEUE_WIDTHS,
            max_chars=40,
            wrap=False,
            interactive=False,
        )
        queue_refresh = gr.Button("Refresh Queue", variant="secondary")

        # ─── Details Section (Row: details scale=2, actions scale=1) ───
        gr.Markdown("## Selected Download")
        with gr.Row():
            with gr.Column(scale=2):
                details_name = gr.Textbox(label="Selected download",
                                          value=EMPTY_SELECTION, interactive=False)
                details_status = gr.Textbox(label="Download status",
                                            value="", interactive=False)
                details_progress = gr.Slider(minimum=0, maximum=100, step=0.1,
                                             label="Complete (%)",
                                             value=0, visible=False,
                                             interactive=False)
                details_size = gr.Textbox(label="Downloaded / Total",
                                          value="— / —", interactive=False)
                details_files = gr.DataFrame(
                    headers=["File path", "Downloaded", "Size"],
                    datatype=["str", "str", "str"],
                    interactive=False,
                    label="Files (empty until metadata is available)",
                )
            with gr.Column(scale=1):
                pause_btn = gr.Button("Pause", variant="secondary", interactive=False)
                resume_btn = gr.Button("Resume", variant="secondary", interactive=False)
                transfer_btn = gr.Button("Transfer", variant="primary", interactive=False)
                delete_btn = gr.Button("Delete", variant="stop", interactive=False)
                confirm_box = gr.Checkbox(
                    label="Confirm deletion of the selected download's files",
                    value=False)
                action_result = gr.Textbox(label="Action result",
                                           value="", interactive=False)

        details_state = gr.State(None)

        detail_core = [details_status, details_progress, details_size,
                       details_files, pause_btn, resume_btn,
                       transfer_btn, delete_btn]

        # ─── Health Section ───
        gr.Markdown("## Health Check")
        health_output = gr.Textbox(
            label="dock-health.py output",
            lines=20,
            max_lines=40,
            autoscroll=False,
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
                return (light, "Connected", country, s.get("city", "—"),
                        uptime, s.get("current_server", "—"), s.get("ip", "—"),
                        s.get("technology", "—"), s.get("protocol", "—"),
                        s.get("transfer", "—"))
            else:
                light = "🔴"
                return (light, f"Disconnected ({s.get('error', 'unknown')})",
                        "", "", "", "", "", "", "", "")

        def load_stats():
            try:
                items = get_download_queue()
            except Exception as exc:
                return (f"Queue unavailable: {exc}", "—", "—", "—", "—")
            active = sum(1 for i in items if i.get("status") in ("downloading", "seeding"))
            stopped = sum(1 for i in items if i.get("status") == "stopped")
            completed = sum(1 for i in items if i.get("status") == "completed")

            def rate(key):
                values = [i.get(key) for i in items if i.get(key) is not None]
                if not values:
                    return "—"
                return format_size(sum(values)) + "/s"

            return (str(active), str(stopped), str(completed),
                    rate("download_speed"), rate("upload_speed"))

        def load_queue():
            try:
                items = get_download_queue()
            except Exception as exc:
                return [["—", "Queue unavailable", "—", "—", "—",
                         "—", "—", "—", str(exc), ""]]
            if not items:
                return [["—", "No downloads in queue", "—", "—", "—",
                         "—", "—", "—", "", ""]]
            return [list(format_job(i).values()) for i in items]

        def load_health():
            return get_health_output()

        def do_rotate():
            rotate_vpn()
            return load_vpn()

        def resolve_selection(name):
            """Select handler follow-up: state + detail panel for a name."""
            state, job = lookup_selection(name)
            values = detail_values(job)
            return (state,) + values[1:]

        def refresh_from_state(state):
            """Timer/action refresh: detail panel for the selected job."""
            job = {}
            if state and state.get("job_id"):
                try:
                    jobs = get_download_queue()
                    job = find_job(jobs, state.get("job_id"),
                                   state.get("gid")) or {}
                    if not job:
                        job = {"name": EMPTY_SELECTION, "status": "stopped",
                               "progress": {},
                               "error": "Selected job is offline or its GID changed; refresh the queue"}
                except Exception as exc:
                    job = {"name": EMPTY_SELECTION, "status": "stopped",
                           "progress": {}, "error": f"Queue unavailable: {exc}"}
            return detail_values(job)[1:]

        def require_state(state):
            if not state or not state.get("job_id") or not state.get("gid"):
                return None, "❌ Select a live download first (click a table row)."
            return state, ""

        def do_pause(state):
            state, err = require_state(state)
            if err:
                return err
            ok, msg = queue_action(state["job_id"], state["gid"], "pause")
            return ("✅ " if ok else "❌ ") + msg

        def do_resume(state):
            state, err = require_state(state)
            if err:
                return err
            ok, msg = queue_action(state["job_id"], state["gid"], "resume")
            return ("✅ " if ok else "❌ ") + msg

        def do_transfer(state):
            state, err = require_state(state)
            if err:
                return err
            ok, msg = queue_action(state["job_id"], state["gid"], "transfer")
            hint = "" if ok else " (only measured completed payloads can transfer)"
            return ("✅ " if ok else "❌ ") + msg + hint

        def do_delete(state, confirmed):
            state, err = require_state(state)
            if err:
                return err, True
            if not confirmed:
                return "❌ Tick the confirm box first — deletion needs explicit confirmation.", True
            ok, msg = queue_action(state["job_id"], state["gid"], "delete", confirm=True)
            return ("✅ " if ok else "❌ ") + msg, False

        # Wire up events
        vpn_refresh.click(load_vpn, outputs=vpn_outputs)
        vpn_rotate.click(do_rotate, outputs=vpn_outputs)
        queue_refresh.click(load_queue, outputs=[queue_table])
        queue_refresh.click(load_stats, outputs=[stats_active, stats_stopped,
                                                stats_completed, stats_dl, stats_ul])
        health_refresh.click(load_health, outputs=[health_output])

        queue_table.select(selected_download, outputs=[details_name])
        details_name.change(resolve_selection, inputs=[details_name],
                            outputs=[details_state] + detail_core)

        pause_btn.click(do_pause, inputs=[details_state], outputs=[action_result]
                        ).then(refresh_from_state, inputs=[details_state],
                               outputs=detail_core)
        resume_btn.click(do_resume, inputs=[details_state], outputs=[action_result]
                         ).then(refresh_from_state, inputs=[details_state],
                                outputs=detail_core)
        transfer_btn.click(do_transfer, inputs=[details_state], outputs=[action_result]
                           ).then(refresh_from_state, inputs=[details_state],
                                  outputs=detail_core)
        delete_btn.click(do_delete, inputs=[details_state, confirm_box],
                         outputs=[action_result, confirm_box]
                         ).then(refresh_from_state, inputs=[details_state],
                                outputs=detail_core)

        demo.load(load_vpn, outputs=vpn_outputs)
        demo.load(load_queue, outputs=[queue_table])
        demo.load(load_stats, outputs=[stats_active, stats_stopped,
                                       stats_completed, stats_dl, stats_ul])

        # Auto-refresh (re-polls the server every REFRESH_INTERVAL seconds)
        timer = gr.Timer(REFRESH_INTERVAL)
        timer.tick(load_vpn, outputs=vpn_outputs)
        timer.tick(load_queue, outputs=[queue_table])
        timer.tick(load_stats, outputs=[stats_active, stats_stopped,
                                        stats_completed, stats_dl, stats_ul])
        timer.tick(refresh_from_state, inputs=[details_state], outputs=detail_core)

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
        theme=gr.themes.Default(),
    )


if __name__ == "__main__":
    main()
