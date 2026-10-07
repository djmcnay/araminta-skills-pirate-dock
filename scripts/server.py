"""
Pirate Dock — API Server

Architecture v3: headless first, with Playwright/Chromium browser fallback
inside the container for human-in-the-loop visual flows.

Phase 1: Anna's Archive — direct MD5-based downloads via mirrors
Phase 2: Torrent search via Jackett (Torznab API) + aria2

All traffic routes through NordVPN.
"""

import os
import json
import asyncio
import subprocess
import signal
import re
import socket
import time
import uuid
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import quote, unquote_plus

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import httpx

# Browser fallback — CDP-driven, connects to persistent Chromium
try:
    from browser_fallback import (
        browser_navigate,
        browser_wait_for_change,
        browser_extract_download,
        browser_status as _bf_status_raw,
    )
    HAS_BROWSER_FALLBACK = True
except ImportError:
    HAS_BROWSER_FALLBACK = False

# Orchestrated download — handles full flow with resume
try:
    from orchestrate import orchestrate_download
    HAS_ORCHESTRATE = True
except ImportError:
    HAS_ORCHESTRATE = False

# ── Config ───────────────────────────────────────────────────
DOWNLOAD_DIR = Path("/downloads")
DATA_DIR = Path("/data")
STATE_DIR = Path(os.getenv("XDG_DATA_HOME", "/root/.local/share")) / "pirate-dock"
STATE_DIR.mkdir(parents=True, exist_ok=True)

JACKETT_PORT = int(os.getenv("JACKETT_PORT", "9118"))  # We pass --Port 9118 to Jackett
JACKETT_BIN = "/opt/jackett/jackett"
JACKETT_API_KEY = os.getenv("JACKETT_API_KEY", "")  # Set after Jackett first run
DEFAULT_TORRENT_CATEGORIES = os.getenv("TORRENT_CATEGORIES", "2000,5000")
DOWNLOAD_PROCESSES: dict[int, subprocess.Popen] = {}
TORRENT_STATUS_FILE = DATA_DIR / "torrent-status-jobs.json"


def _load_torrent_status_jobs() -> dict:
    try:
        data = json.loads(TORRENT_STATUS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_torrent_status_jobs(jobs: dict) -> None:
    temporary = TORRENT_STATUS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(jobs, sort_keys=True))
    temporary.replace(TORRENT_STATUS_FILE)


def _reserve_rpc_port() -> int:
    """Reserve a currently unused localhost aria2 RPC port without count-based reuse."""
    for port in range(6800, 6900):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise HTTPException(status_code=503, detail="No aria2 RPC port is available")


# Anna's Archive mirrors (tried in order)
ANNAS_MIRRORS = [
    "https://annas-archive.gl",
    "https://annas-archive.pk",
    "https://annas-archive.gd",
]

# ── Models ───────────────────────────────────────────────────
class VpnConnectRequest(BaseModel):
    country: str = "South_Africa"
    server: str | None = None

class AnnaDownloadRequest(BaseModel):
    md5: str
    optional_name: str | None = None
    mirror: str | None = None

class AnnaSearchRequest(BaseModel):
    query: str
    mirror: str | None = None

class TorrentMagnetRequest(BaseModel):
    magnet: str
    optional_name: str | None = None
    # aria2 size suffixes: 0 = unlimited, or e.g. "1M", "500K", "10M".
    # Upload is capped by default: be a good peer while downloading, never
    # saturate the Pi's uplink; seed-time=0 means aria2 exits on completion,
    # so there is no post-download seeding at all.
    upload_limit: str = "1M"
    download_limit: str = "0"  # 0 = unlimited (downloads should be fast)

class JackettSearchRequest(BaseModel):
    query: str
    indexer: str = "all"  # "all" searches all configured indexers
    categories: str = DEFAULT_TORRENT_CATEGORIES  # movies + TV by default

class UfcWatchRequest(BaseModel):
    event: str  # e.g. "UFC 327"
    quality: str = "1080"  # preferred quality
    poll_interval: int = 300  # seconds between polls

# ── VPN helpers ──────────────────────────────────────────────
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

def _validate_nordvpn_value(value: str, what: str) -> str:
    """NordVPN server/country names: allowlisted charset, no option-like input."""
    if not value or not _SAFE_VALUE_RE.fullmatch(value):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {what}. Allowed: letters, digits, underscore, dot, hyphen (max 64).",
        )
    if value.startswith("-"):
        raise HTTPException(status_code=400, detail=f"{what} must not look like a CLI option.")
    return value

def _nordvpn(*args: str) -> str:
    r = subprocess.run(
        ["nordvpn", *args],
        capture_output=True, text=True, timeout=30
    )
    return (r.stdout + r.stderr).strip()

def vpn_status() -> dict:
    out = _nordvpn("status")
    connected = "Connected" in out
    ip = ""
    country = ""
    for line in out.splitlines():
        if line.startswith("IP:"):
            ip = line.split(":", 1)[1].strip()
        if line.startswith("Country:"):
            country = line.split(":", 1)[1].strip()
    return {"connected": connected, "ip": ip, "country": country, "raw": out}

def vpn_check():
    """Raise if VPN is not connected."""
    s = vpn_status()
    if not s["connected"]:
        raise HTTPException(
            status_code=503,
            detail=f"VPN not connected. Use POST /vpn/connect first. Status: {s['raw']}"
        )

# ── Jackett helpers ──────────────────────────────────────────
# Jackett lifecycle is managed by run.sh watchdog (auto-restarts on exit).
# server.py only verifies it's alive and provides API access.

def _kill_jackett() -> bool:
    """Kill the Jackett process so watchdog restarts it. Returns True if killed."""
    import subprocess as sp
    r = sp.run(["pgrep", "-f", "jackett"], capture_output=True, text=True)
    pids = r.stdout.strip().split()
    if not pids:
        return False
    for pid in pids:
        try:
            import os as _os
            _os.kill(int(pid), 9)
        except Exception:
            pass
    return True

def _jackett_url(path: str = "") -> str:
    base = f"http://127.0.0.1:{JACKETT_PORT}"
    if not JACKETT_API_KEY:
        return f"{base}{path}"
    sep = "&" if "?" in path else "?"
    return f"{base}{path}{sep}apikey={JACKETT_API_KEY}"

def _ensure_jackett_api_key() -> None:
    """Refresh Jackett API key from disk or raise a structured service error."""
    global JACKETT_API_KEY
    if not JACKETT_API_KEY:
        JACKETT_API_KEY = _jackett_api_key() or ""
    if not JACKETT_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Jackett API key not available. Open Jackett once or set JACKETT_API_KEY.",
        )

def _jackett_api_key() -> str | None:
    """Get Jackett API key from its config file."""
    for cfg_file in [DATA_DIR / "jackett" / "ServerConfig.json",
                     DATA_DIR / "jackett" / "appsettings.json"]:
        if cfg_file.exists():
            try:
                cfg = json.loads(cfg_file.read_text())
                key = cfg.get("APIKey")
                if key:
                    return key
            except Exception:
                pass
    return None

def _local_http_alive(port: int, path: str = "/") -> bool:
    """Return true when a localhost HTTP service is reachable."""
    try:
        r = httpx.get(f"http://127.0.0.1:{port}{path}", timeout=2, follow_redirects=False)
        return r.status_code < 500
    except Exception:
        return False

# ── App lifecycle ────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: verify Jackett is alive (started by run.sh watchdog).
    global JACKETT_API_KEY
    if not JACKETT_API_KEY:
        JACKETT_API_KEY = _jackett_api_key() or ""
    import time
    for _ in range(30):
        if _local_http_alive(JACKETT_PORT, "/api/v2.0/indexers"):
            break
        time.sleep(1)
    yield
    # Shutdown: nothing to clean up (watchdog handles Jackett)

app = FastAPI(title="Pirate Dock", version="3.0", lifespan=lifespan)

# ── VPN endpoints ────────────────────────────────────────────
@app.get("/status")
async def get_status():
    s = vpn_status()
    s["jackett_running"] = _local_http_alive(JACKETT_PORT, "/api/v1.0/server/config")
    s["jackett_port"] = JACKETT_PORT
    s["jackett_api_key_set"] = bool(JACKETT_API_KEY)
    s["display_url"] = os.getenv("DISPLAY_URL", "https://araminta.taild3f7b9.ts.net/pirate/")
    return s

@app.post("/vpn/connect")
async def vpn_connect(req: VpnConnectRequest):
    if req.server:
        server = _validate_nordvpn_value(req.server, "server")
        result = _nordvpn("connect", server)
    else:
        country = _validate_nordvpn_value(req.country, "country")
        result = _nordvpn("connect", "--group", "P2P", country)
    return {"result": result, "status": vpn_status()}

@app.post("/vpn/disconnect")
async def vpn_disconnect():
    return {"result": _nordvpn("disconnect")}

# ── Anna's Archive — Search ──────────────────────────────────
@app.get("/search/annas-archive")
async def search_annas_get(q: str, mirror: str | None = None):
    """Search Anna's Archive (GET for convenience)."""
    return await _search_annas(q, mirror)

@app.post("/search/annas-archive")
async def search_annas_post(req: AnnaSearchRequest):
    """Search Anna's Archive."""
    return await _search_annas(req.query, req.mirror)

async def _search_annas(query: str, mirror: str | None = None):
    """Scrape Anna's Archive search results page."""
    mirrors = [mirror] if mirror else ANNAS_MIRRORS
    search_url_template = "{}/search?q={}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }

    for base in mirrors:
        url = search_url_template.format(base, quote(query))
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    continue

                results = _parse_annas_search(resp.text, base)
                if results:
                    return {
                        "query": query,
                        "mirror": base,
                        "results": results[:20],  # Cap at 20
                        "count": len(results),
                    }
        except Exception as e:
            continue

    return {
        "query": query,
        "results": [],
        "count": 0,
        "error": "No results from any mirror (all mirrors may be down)",
    }

def _parse_annas_search(html: str, base_url: str) -> list:
    """Parse Anna's Archive search results HTML (updated 2026-04-14 for new UI)."""
    from bs4 import BeautifulSoup
    import re

    soup = BeautifulSoup(html, "lxml")
    results = []

    # New structure (2026-04): div.js-aarecord-list-outer is the main container
    # Each result row is a child div with flex/border-b classes
    container = soup.select_one(".js-aarecord-list-outer")
    if container:
        rows = [c for c in container.children
                if hasattr(c, 'get') and 'border-b' in ' '.join(c.get('class', []))]
    else:
        # Fallback to old class names
        rows = soup.select('.js-search-result, .search-result')

    for row in rows:
        try:
            link = row.select_one("a[href*='/md5/'], a[href*='/isbn/'], a[href*='/doi/']")
            if not link:
                continue

            href = link.get("href", "")
            md5 = ""
            for part in href.split("/"):
                if len(part) == 32 and all(c in "0123456789abcdef" for c in part):
                    md5 = part
                    break

            # Title and details from info div
            info_div = row.select_one("div.max-w-full, div.overflow-hidden")
            title = "Unknown"
            size = ""
            details = ""

            if info_div:
                texts = []
                for child in info_div.descendants:
                    if hasattr(child, 'name') and child.name in ('div', 'span', 'a', 'p', 'h3'):
                        t = child.get_text(strip=True)
                        if t and t not in texts:
                            texts.append(t)

                full_text = info_div.get_text(separator=" ", strip=True)

                # Title: second meaningful text chunk (first is often filename)
                if len(texts) >= 2:
                    title = texts[1]
                elif texts:
                    title = texts[0]

                # Size
                size_match = re.search(r'(\d+[\.,]?\d*)\s*(MB|KB|GB)', full_text)
                if size_match:
                    size = size_match.group(0)

                details = full_text[:120]

            # Source library
            source = ""
            source_img = row.select_one("img[alt]")
            if source_img:
                source = source_img.get("alt", "")

            if md5:
                results.append({
                    "md5": md5,
                    "title": title,
                    "size": size,
                    "details": details,
                    "source": source,
                    "download_url": f"{base_url}/md5/{md5}",
                })
        except Exception:
            continue

    # Fallback: regex for MD5 hashes if structured parsing found nothing
    if not results:
        md5_pattern = re.compile(r'/md5/([a-f0-9]{32})')
        seen = set()
        for match in md5_pattern.finditer(html):
            md5 = match.group(1)
            if md5 not in seen:
                seen.add(md5)
                results.append({
                    "md5": md5,
                    "title": "",
                    "size": "",
                    "details": "",
                    "source": "",
                    "download_url": f"{base_url}/md5/{md5}",
                })

    return results

# ── Anna's Archive — Download ────────────────────────────────
@app.post("/download/annas-archive")
async def download_annas(req: AnnaDownloadRequest):
    """
    Download a book from Anna's Archive by MD5.

    Strategy:
    1. Try to get a direct download link from the mirror
    2. If the book is in a torrent we know about, use magnet
    3. Otherwise, construct the page URL for reference

    Without an API key, direct download from slow servers requires
    a browser for CAPTCHA. This endpoint gives you the page URL
    and MD5 so you can download manually, OR if the book is
    available via a torrent mirror, it'll start the torrent download.
    """
    vpn_check()

    md5 = req.md5
    mirror_base = req.mirror or ANNAS_MIRRORS[0]

    # The direct page URL
    page_url = f"{mirror_base}/md5/{md5}"

    # Try to fetch the page and extract any direct download links
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }

    download_info = {
        "md5": md5,
        "page_url": page_url,
        "mirror": mirror_base,
        "status": "info",
    }

    headless_ok = False
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(page_url, headers=headers)
            if resp.status_code == 200:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(resp.text, "lxml")

                title_el = soup.select_one('h1, .book-title, .md5card-title')
                if title_el:
                    download_info["title"] = title_el.get_text(strip=True)

                # Check for CAPTCHA / challenge markers
                page_text = resp.text.lower()
                challenge = any(blocker in page_text for blocker in [
                    "captcha", "ddos-guard", "challenge",
                    "just a moment", "please verify", "are you human",
                ])

                for link in soup.select('a[href]'):
                    href = link.get("href", "")
                    text = link.get_text(strip=True).lower()
                    if any(kw in text for kw in ["download", "pdf", "epub", "mobi", "slow"]):
                        if href.startswith("http"):
                            download_info.setdefault("download_links", []).append({
                                "text": link.get_text(strip=True),
                                "url": href,
                            })

                if challenge and not download_info.get("download_links"):
                    download_info["status"] = "headless_blocked"
                    download_info["reason"] = "CAPTCHA/challenge detected, no direct links found"
                else:
                    download_info["status"] = "page_loaded"
                    headless_ok = True
            else:
                download_info["status"] = "headless_blocked"
                download_info["reason"] = f"HTTP {resp.status_code}"
    except Exception as e:
        download_info["status"] = "headless_blocked"
        download_info["reason"] = str(e)

    # ── Phase 2: Browser fallback ───────────────────────────────
    if headless_ok and download_info.get("download_links"):
        return download_info

    if not HAS_BROWSER_FALLBACK:
        download_info["status"] = "blocked_no_browser"
        download_info["message"] = (
            "CAPTCHA blocked headless scraping and browser fallback is not available. "
            f"Manual download: {page_url}"
        )
        return download_info

    try:
        browser_result = await browser_navigate(md5, mirror=mirror_base)

        download_info["method"] = "cdp_navigate"
        download_info["browser_state"] = browser_result.get("state")

        if browser_result.get("status") == "ok":
            if browser_result.get("state") == "download_ready":
                download_info["download_links"] = browser_result.get("download_links", [])
                download_info["status"] = "success"
                download_info["message"] = "Download links found via browser."
            elif browser_result.get("state") == "captcha_visual":
                download_info["status"] = "captcha_waiting"
                download_info["message"] = browser_result.get("message")
                if "display_url" in browser_result:
                    download_info["display_url"] = browser_result["display_url"]
                if "screenshot_path" in browser_result:
                    download_info["screenshot_path"] = browser_result["screenshot_path"]
                if "screenshot_b64" in browser_result:
                    download_info["screenshot_b64"] = browser_result["screenshot_b64"]
            else:
                download_info["status"] = "browser_" + browser_result.get("state", "unknown")
                download_info["message"] = browser_result.get("message", "Unknown browser state")
        else:
            download_info["status"] = "browser_error"
            download_info["message"] = browser_result.get("message", "Browser navigation failed")

    except Exception as e:
        download_info["status"] = "browser_error"
        download_info["message"] = f"Browser fallback failed: {e}"

    return download_info

# ── Anna's Archive — Direct MD5 Download ─────────────────────
@app.get("/download/annas-archive/{md5}")
async def download_annas_md5(md5: str, name: str | None = None, mirror: str | None = None):
    """Convenience: GET /download/annas-archive/{md5}?name=MyBook"""
    req = AnnaDownloadRequest(md5=md5, optional_name=name, mirror=mirror)
    return await download_annas(req)

# ── Browser status (persistent Chromium CDP) ──────────────────
@app.get("/browser/status")
async def browser_status():
    """Check if persistent Chromium CDP is reachable."""
    if not HAS_BROWSER_FALLBACK:
        return {"available": False, "reason": "browser_fallback not importable"}
    return await _bf_status_raw()

# ── Anna's Archive — three-step browser flow ────────────────────
# Step 1: Navigate → detect state
@app.post("/download/annas-archive/browser")
async def download_annas_browser(req: AnnaDownloadRequest):
    """
    Navigate to the Anna's Archive book page via persistent Chromium CDP.
    Returns page state: captcha_visual, ddos_guard_js, countdown, download_ready, etc.
    POST body: { "md5": "...", "mirror": "..." (optional) }
    """
    if not HAS_BROWSER_FALLBACK:
        raise HTTPException(
            status_code=501,
            detail="Browser fallback not available."
        )
    mirror = req.mirror or ANNAS_MIRRORS[0]
    vpn_check()
    return await browser_navigate(req.md5, mirror=mirror)


# ── Orchestrated download (replaces three-step for most uses) ──

class OrchestrateRequest(BaseModel):
    md5: str
    mirror: str | None = None
    resume: bool = False


@app.post("/download/annas-archive/orchestrate")
async def download_annas_orchestrate(req: OrchestrateRequest):
    """
    Complete end-to-end download with resume capability.

    Fresh start (resume=false): navigates book page → clicks Slow Partner #1
      → handles DDoS-Guard → detects hCaptcha → returns captcha_required + VNC URL

    Resume (resume=true): reconnects to the existing browser page on the
      slow_download URL, polls for page change after David solves captcha,
      waits for countdown → finds token URL → curls file → returns success

    POST body: { "md5": "d4094b...", "resume": false }
    """
    if not HAS_ORCHESTRATE:
        raise HTTPException(status_code=501, detail="Orchestrate module not available")
    mirror = req.mirror or ANNAS_MIRRORS[0]
    vpn_check()
    return await orchestrate_download(req.md5, mirror=mirror, resume=req.resume)

@app.get("/download/annas-archive/{md5}/browser")
async def download_annas_browser_md5(md5: str, mirror: str | None = None):
    """Convenience GET for browser navigate."""
    if not HAS_BROWSER_FALLBACK:
        raise HTTPException(
            status_code=501,
            detail="Browser fallback not available."
        )
    m = mirror or ANNAS_MIRRORS[0]
    vpn_check()
    return await browser_navigate(md5, mirror=m)

# Step 2: Wait for CAPTCHA solve → detect page change
@app.post("/download/annas-archive/browser/wait")
async def download_annas_browser_wait(req: AnnaDownloadRequest, timeout: int = 120):
    """
    Wait for the page to change after David solves the CAPTCHA via VNC.
    POST body: { "md5": "...", "mirror": "..." (optional) }
    Query param: timeout=120 (seconds)
    """
    if not HAS_BROWSER_FALLBACK:
        raise HTTPException(
            status_code=501,
            detail="Browser fallback not available."
        )
    mirror = req.mirror or ANNAS_MIRRORS[0]
    vpn_check()
    return await browser_wait_for_change(req.md5, mirror=mirror, timeout=timeout)

@app.get("/download/annas-archive/{md5}/browser/wait")
async def download_annas_browser_wait_md5(
    md5: str, mirror: str | None = None, timeout: int = 120
):
    """Convenience GET for browser wait."""
    if not HAS_BROWSER_FALLBACK:
        raise HTTPException(
            status_code=501,
            detail="Browser fallback not available."
        )
    m = mirror or ANNAS_MIRRORS[0]
    vpn_check()
    return await browser_wait_for_change(md5, mirror=m, timeout=timeout)

# Step 3: Extract download URL and curl file
@app.post("/download/annas-archive/browser/extract")
async def download_annas_browser_extract(req: AnnaDownloadRequest, timeout: int = 180):
    """
    Click download button, wait for token URL, curl file to /downloads.
    POST body: { "md5": "...", "mirror": "..." (optional) }
    Query param: timeout=180 (seconds)
    """
    if not HAS_BROWSER_FALLBACK:
        raise HTTPException(
            status_code=501,
            detail="Browser fallback not available."
        )
    mirror = req.mirror or ANNAS_MIRRORS[0]
    vpn_check()
    return await browser_extract_download(req.md5, mirror=mirror, timeout=timeout)

@app.get("/download/annas-archive/{md5}/browser/extract")
async def download_annas_browser_extract_md5(
    md5: str, mirror: str | None = None, timeout: int = 180
):
    """Convenience GET for browser extract."""
    if not HAS_BROWSER_FALLBACK:
        raise HTTPException(
            status_code=501,
            detail="Browser fallback not available."
        )
    m = mirror or ANNAS_MIRRORS[0]
    vpn_check()
    return await browser_extract_download(md5, mirror=m, timeout=timeout)

# ── Torrent search — Jackett ─────────────────────────────────
@app.get("/search/torrents")
async def search_torrents_get(
    q: str,
    indexer: str = "all",
    categories: str = DEFAULT_TORRENT_CATEGORIES,
    cat: str | None = None,
):
    return await _search_jackett(q, indexer, cat or categories)

@app.post("/search/torrents")
async def search_torrents_post(req: JackettSearchRequest):
    return await _search_jackett(req.query, req.indexer, req.categories)

def _validate_torrent_categories(categories: str) -> str:
    """Allow Torznab category lists such as 2000 or 2000,5000."""
    categories = categories.strip()
    if not re.fullmatch(r"\d+(,\d+)*", categories):
        raise HTTPException(
            status_code=400,
            detail="categories must be a comma-separated list of numeric Torznab category IDs",
        )
    return categories

async def _search_jackett(query: str, indexer: str = "all", categories: str = DEFAULT_TORRENT_CATEGORIES):
    """Search torrents via Jackett Torznab API."""
    vpn_check()
    _ensure_jackett_api_key()
    categories = _validate_torrent_categories(categories)

    url = _jackett_url(
        f"/api/v2.0/indexers/{indexer}/results/torznab/api"
        f"?t=search&cat={categories}&q={quote(query)}"
    )

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            resp = await client.get(url)
            if resp.status_code in (301, 302, 303, 307, 308):
                raise HTTPException(
                    status_code=503,
                    detail="Jackett redirected instead of returning API data; check JACKETT_API_KEY",
                )
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"Jackett returned {resp.status_code}"
                )

            results = _parse_torznab(resp.text)
            return {
                "query": query,
                "indexer": indexer,
                "categories": categories,
                "results": results[:20],
                "count": len(results),
            }
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Jackett search timed out")
    except httpx.ConnectError:
        raise HTTPException(
            status_code=503,
            detail="Jackett not running. Check /status endpoint."
        )

def _parse_torznab(xml_text: str) -> list:
    """Parse Torznab XML response into structured results."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(xml_text, "lxml-xml")
    results = []

    def text_value(item, tag: str, default: str = "") -> str:
        node = item.select_one(tag)
        return node.get_text(strip=True) if node else default

    def int_value(value: str | None) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    for item in soup.select("item"):
        attrs = {
            attr.get("name", "").lower(): attr.get("value", "")
            for attr in item.find_all(lambda tag: tag.name and tag.name.endswith("attr"))
            if attr.get("name")
        }

        magnet = ""
        torrent_url = ""

        if attrs.get("magneturl", "").startswith("magnet:"):
            magnet = attrs["magneturl"]

        for enclosure in item.select("enclosure"):
            url = enclosure.get("url", "")
            if url.startswith("magnet:") and not magnet:
                magnet = url
            elif enclosure.get("type") == "application/x-bittorrent" or url.endswith(".torrent"):
                torrent_url = torrent_url or url

        for link in item.select("link"):
            link_text = link.get_text(strip=True)
            link_url = link.get("url", "") or link_text
            if link_url.startswith("magnet:") and not magnet:
                magnet = link_url
                break
            if link_url and not torrent_url:
                torrent_url = link_url

        results.append({
            "title": text_value(item, "title"),
            "magnet": magnet,
            "torrent": torrent_url,
            "size": int_value(attrs.get("size") or text_value(item, "size")),
            "seeders": int_value(attrs.get("seeders") or text_value(item, "seeders")),
            "peers": int_value(attrs.get("peers") or text_value(item, "peers")),
            "source": attrs.get("jackettindexer") or text_value(item, "source") or text_value(item, "jackettindexer"),
            "link": text_value(item, "link"),
        })

    return results

# ── Legacy search endpoints (via Jackett) ────────────────────
@app.get("/search/piratebay")
async def search_piratebay(q: str):
    return await _search_jackett(q, "thepiratebay")

@app.get("/search/1337x")
async def search_1337x(q: str):
    return await _search_jackett(q, "1337x")

@app.get("/search/ext")
async def search_ext(q: str):
    return await _search_jackett(q, "ext")

# ── Torrent download via aria2 ───────────────────────────────
def _validate_magnet_uri(magnet: str) -> str:
    magnet = magnet.strip()
    if not magnet.startswith("magnet:?"):
        raise HTTPException(status_code=400, detail="magnet must start with magnet:?")
    magnet_lower = magnet.lower()
    if "xt=urn:btih:" not in magnet_lower and "xt=urn:btmh:" not in magnet_lower:
        raise HTTPException(status_code=400, detail="magnet must include xt=urn:btih or xt=urn:btmh")
    if any(ord(ch) < 32 for ch in magnet):
        raise HTTPException(status_code=400, detail="magnet contains invalid control characters")
    return magnet

def _validate_download_name(name: str | None) -> str | None:
    if not name:
        return None
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise HTTPException(status_code=400, detail="optional_name must be a filename, not a path")
    if any(ord(ch) < 32 for ch in name):
        raise HTTPException(status_code=400, detail="optional_name contains invalid control characters")
    return name

# aria2 size-rate: plain bytes, K (KiB), M (MiB), or 0 = unlimited.
_ARA_RATE_RE = re.compile(r"^0|[1-9][0-9]*[KM]?$")

def _validate_ara_rate(value: str, what: str) -> str:
    """aria2 --max-*-limit format: '0' (unlimited) or integer + optional K/M."""
    if not _ARA_RATE_RE.fullmatch(value or ""):
        raise HTTPException(
            status_code=400,
            detail=f"{what} must be '0' (unlimited) or a positive integer with optional K/M suffix (e.g. 500K, 10M)",
        )
    return value

def _prune_download_processes() -> None:
    for pid, proc in list(DOWNLOAD_PROCESSES.items()):
        if proc.poll() is not None:
            DOWNLOAD_PROCESSES.pop(pid, None)

@app.post("/download/magnet")
async def download_magnet(req: TorrentMagnetRequest):
    """Download a torrent via aria2."""
    vpn_check()
    magnet = _validate_magnet_uri(req.magnet)
    optional_name = _validate_download_name(req.optional_name)
    upload_limit = _validate_ara_rate(req.upload_limit, "upload_limit")
    download_limit = _validate_ara_rate(req.download_limit, "download_limit")

    # Per-job log: aria2 diagnostics were previously lost to DEVNULL, which
    # is why the UFC stall was invisible for 40 minutes. Each job gets its
    # own log file; /downloads/active surfaces the paths.
    # One persistent record per launch lets status bind an aria2 RPC endpoint to
    # its own launch time.  It is deliberately keyed by RPC port, never title.
    job_log = DOWNLOAD_DIR / f".aria2-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.log"
    rpc_port = _reserve_rpc_port()
    started_at = time.time()
    cmd = [
        "aria2c",
        "--seed-time=0",
        "--dir=/downloads",
        "--summary-interval=10",
        "--max-connection-per-server=4",
        "--split=4",
        "--enable-rpc=true",
        "--rpc-listen-all=false",
        "--rpc-secret=piratedockrpc",
        "--allow-overwrite=false",
        "--continue=true",
        "--log", str(job_log),
        "--log-level=info",
        f"--rpc-listen-port={rpc_port}",
        f"--max-upload-limit={upload_limit}",
        "--max-overall-upload-limit=" + upload_limit,
        "--max-download-limit=" + download_limit,
        "--max-overall-download-limit=" + download_limit,
    ]
    if optional_name:
        cmd.extend(["--out", optional_name])
    cmd.append(magnet)

    r = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    DOWNLOAD_PROCESSES[r.pid] = r
    jobs = _load_torrent_status_jobs()
    jobs[str(rpc_port)] = {
        "job_id": uuid.uuid4().hex,
        "pid": r.pid,
        "started_at": started_at,
        "rpc_port": rpc_port,
        "log_file": str(job_log),
        "optional_name": optional_name,
    }
    _save_torrent_status_jobs(jobs)

    return {
        "status": "started",
        "magnet": magnet[:80] + "...",
        "output_dir": "/downloads",
        "pid": r.pid,
        "log_file": str(job_log),
        "limits": {"upload": upload_limit, "download": download_limit},
    }

def _queue_item(job: dict, live: dict | None, error: str | None = None) -> dict:
    """Normalize measured telemetry; never infer bytes from allocated files."""
    name = job.get("optional_name") or job.get("name") or f"Job {job.get('job_id', '?')}"
    item = {"job_id": job.get("job_id"), "rpc_port": job.get("rpc_port"),
            "started_at": job.get("started_at"), "name": name, "status": "stopped",
            "progress": {"downloaded": None, "total": None, "percent": None},
            "download_speed": None, "upload_speed": None, "connections": None,
            "error": error, "error_code": None, "telemetry_available": False, "files": []}
    if live is None:
        return item
    info = live.get("bittorrent", {}).get("info", {})
    files = live.get("files", [])
    item["files"] = [{"path": f.get("path", "").removeprefix("/downloads/"),
                      "length": int(f.get("length", 0)),
                      "completed": int(f.get("completedLength", 0))}
                     for f in files if f.get("path") and not live.get("bittorrent", {}).get("mode") == "metadata"]
    item["name"] = info.get("name") or (Path(files[0]["path"]).name if files and files[0].get("path") else name)
    total = int(live.get("totalLength", 0))
    downloaded = int(live.get("completedLength", 0))
    raw_status = live.get("status", "removed")
    item.update({"gid": live.get("gid"), "aria2_status": raw_status,
                 "status": "completed" if raw_status == "complete" else
                 "seeding" if raw_status == "active" and live.get("seeder") == "true" else
                 "downloading" if raw_status in ("active", "waiting") else "stopped",
                 "progress": {"downloaded": downloaded, "total": total,
                              "percent": downloaded / total * 100 if total else None},
                 "download_speed": int(live.get("downloadSpeed", 0)),
                 "upload_speed": int(live.get("uploadSpeed", 0)),
                 "connections": int(live.get("connections", 0)),
                 "error": live.get("errorMessage") or error,
                 "error_code": live.get("errorCode"), "telemetry_available": True})
    return item


async def _queue_job(client, job: dict) -> list:
    """Read a launch's own RPC process, retaining offline launch records."""
    job = dict(job)
    if not job.get("optional_name"):
        try:
            with Path(job.get("log_file", "")).open("rb") as log:
                text = log.read(65536)
                log.seek(max(0, log.seek(0, 2) - 65536))
                text += log.read(65536)
            names = re.findall(r"Download (?:GID#\w+ not complete|complete): (.+)", text.decode(errors="replace"))
            if names:
                job["name"] = unquote_plus(names[-1].removeprefix("[METADATA]"))
        except OSError:
            pass
    try:
        # A recycled PID/port must never attach another launch's telemetry.
        cmd = Path(f"/proc/{int(job['pid'])}/cmdline").read_bytes().split(b"\0")
        if b"aria2c" not in [Path(p.decode(errors="replace")).name.encode() for p in cmd[:1]] or str(job['log_file']).encode() not in cmd:
            raise ValueError("Tracked aria2 process is no longer running")
        port = int(job['rpc_port'])
        if not 6800 <= port < 6900:
            raise ValueError("Invalid tracked RPC port")
        live = []
        for method in ("tellActive", "tellWaiting", "tellStopped"):
            offset = 0
            while True:
                params = ["token:piratedockrpc"] + ([] if method == "tellActive" else [offset, 100])
                response = await client.post(f"http://127.0.0.1:{port}/jsonrpc", json={
                    "jsonrpc": "2.0", "id": "queue", "method": f"aria2.{method}", "params": params})
                response.raise_for_status()
                data = response.json()
                if "error" in data:
                    raise ValueError(str(data['error']))
                page = data['result']
                live.extend(page)
                if method == "tellActive" or len(page) < 100:
                    break
                offset += 100
                if offset >= 10000:
                    raise ValueError("RPC history exceeds queue read limit")
        # Completed magnet-metadata rows are not completed payload downloads.
        live = [row for row in live if not row.get('followedBy')]
        if live:
            return [_queue_item(job, row) for row in live]
        return [_queue_item(job, None, "No live RPC records; historical outcome unknown")]
    except (OSError, ValueError, KeyError, httpx.HTTPError) as exc:
        return [_queue_item(job, None, f"Telemetry unavailable: {exc}")]


@app.get("/queue")
async def get_queue():
    """Read-only launch history plus live telemetry; unknown values are null."""
    tracked = _load_torrent_status_jobs()
    if not tracked:
        return {"jobs": [], "total": 0}
    async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
        groups = await asyncio.gather(*(_queue_job(client, job) for job in tracked.values()))
    jobs = [job for group in groups for job in group]
    return {"jobs": jobs, "total": len(jobs)}


# ── Selected-job controls ────────────────────────────────────
class QueueActionRequest(BaseModel):
    gid: str = Field(pattern=r"^[0-9a-fA-F]{16}$")
    confirm: bool = False
    dry_run: bool = False


async def _action_job(job_id: str, gid: str):
    tracked = next((j for j in _load_torrent_status_jobs().values() if j.get("job_id") == job_id), None)
    if tracked is None:
        raise HTTPException(404, "Unknown job")
    async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
        rows = await _queue_job(client, tracked)
    row = next((r for r in rows if r.get("gid") == gid and r.get("telemetry_available")), None)
    if row is None:
        raise HTTPException(409, "Job is offline or its GID changed; refresh the queue")
    return tracked, row


async def _action_rpc(job_id: str, gid: str, method: str):
    tracked, row = await _action_job(job_id, gid)
    permitted = {"pause": {"active", "waiting"}, "unpause": {"paused"},
                 "remove": {"paused"}, "removeDownloadResult": {"complete", "error", "removed"}}
    if row.get("aria2_status") not in permitted[method]:
        raise HTTPException(409, f"Cannot {method} a {row.get('aria2_status')} job")
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        response = await client.post(f"http://127.0.0.1:{int(tracked['rpc_port'])}/jsonrpc", json={
            "jsonrpc": "2.0", "id": "action", "method": f"aria2.{method}",
            "params": ["token:piratedockrpc", gid]})
        response.raise_for_status()
        result = response.json()
    if "error" in result:
        raise HTTPException(409, str(result["error"]))
    return result["result"]


@app.post("/queue/{job_id}/pause")
async def queue_pause(job_id: str, req: QueueActionRequest):
    await _action_rpc(job_id, req.gid, "pause")
    return {"message": "Pause requested. Refreshing measured status."}


@app.post("/queue/{job_id}/resume")
async def queue_resume(job_id: str, req: QueueActionRequest):
    await _action_rpc(job_id, req.gid, "unpause")
    return {"message": "Resume requested. Refreshing measured status."}


def _payload_paths(row):
    root = DOWNLOAD_DIR.resolve()
    paths = []
    for entry in row.get("files", []):
        relative = Path(entry["path"])
        if relative.is_absolute() or not relative.parts or any(p in ("..", ".") or p.startswith('.') for p in relative.parts):
            raise HTTPException(409, "Unsafe payload path")
        path = root / relative
        if any(p.is_symlink() for p in [path, *path.parents] if p != root and p.is_relative_to(root)):
            raise HTTPException(409, "Symlink payloads are not actionable")
        if not path.resolve().is_relative_to(root) or not path.is_file():
            raise HTTPException(409, "Payload is missing or outside downloads")
        paths.append(path)
    if not paths:
        raise HTTPException(409, "No verified payload files available")
    return paths


@app.post("/queue/{job_id}/transfer")
async def queue_transfer(job_id: str, req: QueueActionRequest):
    _, row = await _action_job(job_id, req.gid)
    if row.get("aria2_status") != "complete":
        raise HTTPException(409, "Only a measured completed payload can be transferred")
    paths = _payload_paths(row)
    tops = {p.relative_to(DOWNLOAD_DIR.resolve()).parts[0] for p in paths}
    if len(tops) != 1:
        raise HTTPException(409, "Payload spans multiple dock items; transfer manually")
    item = next(iter(tops))
    if req.dry_run:
        return {"status": "dry-run", "item": item, "files": row["files"]}
    spool = DOWNLOAD_DIR / ".dashboard-transfers"
    if not spool.is_dir():
        raise HTTPException(503, "Dashboard transfer worker is not running")
    request_id = f"{job_id}-{req.gid}"
    request_path = spool / f"{request_id}.request.json"
    result_path = spool / f"{request_id}.result.json"
    if not request_path.exists():
        temporary = spool / f"{request_id}.{uuid.uuid4().hex}.tmp"
        temporary.write_text(json.dumps({"job_id": job_id, "gid": req.gid, "item": item, "files": row["files"]}))
        temporary.replace(request_path)
    result = json.loads(result_path.read_text()) if result_path.exists() else {"status": "queued"}
    return {"request_id": request_id, "message": f"Transfer {result['status']}. Source files are retained.", **result}


@app.get("/transfers/{request_id}")
async def transfer_status(request_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}-[0-9a-fA-F]{16}", request_id):
        raise HTTPException(400, "Invalid transfer ID")
    spool = DOWNLOAD_DIR / ".dashboard-transfers"
    result = spool / f"{request_id}.result.json"
    if result.exists():
        return json.loads(result.read_text())
    if (spool / f"{request_id}.request.json").exists():
        return {"status": "queued"}
    raise HTTPException(404, "Unknown transfer")


@app.post("/queue/{job_id}/delete")
async def queue_delete(job_id: str, req: QueueActionRequest):
    if not req.confirm:
        raise HTTPException(400, "Explicit deletion confirmation required")
    _, row = await _action_job(job_id, req.gid)
    if row.get("aria2_status") not in ("paused", "complete", "error"):
        raise HTTPException(409, "Pause this download before deleting it")
    paths = _payload_paths(row)
    # Refuse shared payloads: a duplicate launch must not lose its files.
    queue = await get_queue()
    selected = {str(p.relative_to(DOWNLOAD_DIR.resolve())) for p in paths}
    for other in queue["jobs"]:
        if (other.get("job_id"), other.get("gid")) != (job_id, req.gid):
            if selected.intersection(f["path"] for f in other.get("files", [])):
                raise HTTPException(409, "Another tracked job references these files")
    spool = DOWNLOAD_DIR / ".dashboard-transfers"
    for request_path in spool.glob("*.request.json"):
        request = json.loads(request_path.read_text())
        if selected.intersection(f["path"] for f in request.get("files", [])):
            result_path = request_path.with_name(request_path.name.replace('.request.json', '.result.json'))
            if not result_path.exists() or json.loads(result_path.read_text()).get("status") not in ("completed", "failed"):
                raise HTTPException(409, "A transfer of these files is queued or running")
    if req.dry_run:
        return {"status": "dry-run", "files": sorted(selected)}
    await _action_rpc(job_id, req.gid, "remove" if row["aria2_status"] == "paused" else "removeDownloadResult")
    # Delete only the enumerated payload files, never a recursive directory or logs.
    for path in _payload_paths(row):
        path.unlink()
    return {"message": f"Deleted {len(paths)} payload files. Empty folders and resume controls retained."}


# ── Download management ─────────────────────────────────────
@app.get("/downloads/status-jobs")
async def status_jobs():
    """Persistent launch metadata for the read-only torrent status client."""
    return {"jobs": _load_torrent_status_jobs()}


@app.get("/downloads/active")
async def active_downloads():
    """List running aria2 processes (with per-job log paths)."""
    _prune_download_processes()
    r = subprocess.run(
        ["pgrep", "-a", "aria2c"],
        capture_output=True, text=True
    )
    logs = sorted(
        ({"name": p.name, "size": p.stat().st_size, "modified": p.stat().st_mtime}
         for p in DOWNLOAD_DIR.glob(".aria2-*.log")),
        key=lambda x: x["name"],
    )
    return {
        "processes": r.stdout.strip() or "none",
        "tracked_pids": sorted(DOWNLOAD_PROCESSES),
        "job_logs": logs[-5:],
    }

@app.delete("/downloads/active/{pid}")
async def cancel_download(pid: int):
    """Cancel a tracked aria2 download process."""
    _prune_download_processes()
    proc = DOWNLOAD_PROCESSES.get(pid)
    if not proc:
        raise HTTPException(status_code=404, detail="download process not tracked or already finished")
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    DOWNLOAD_PROCESSES.pop(pid, None)
    return {"status": "cancelled", "pid": pid}

@app.get("/downloads/list")
async def list_downloads():
    """List files in /downloads."""
    files = []
    if DOWNLOAD_DIR.exists():
        for p in sorted(DOWNLOAD_DIR.iterdir()):
            stat = p.stat()
            files.append({
                "name": p.name,
                "size": stat.st_size,
                "isDir": p.is_dir(),
                "modified": stat.st_mtime,
            })
    return {"downloads": files}

# ── Jackett management ───────────────────────────────────────
@app.get("/jackett/indexers")
async def jackett_indexers():
    """List available Jackett indexers."""
    _ensure_jackett_api_key()
    url = _jackett_url("/api/v2.0/indexers")
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            resp = await client.get(url)
            if resp.status_code in (301, 302, 303, 307, 308):
                raise HTTPException(
                    status_code=503,
                    detail="Jackett redirected instead of returning API data; check JACKETT_API_KEY",
                )
            if resp.status_code != 200:
                raise HTTPException(status_code=502, detail=f"Jackett returned {resp.status_code}")
            return resp.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Jackett unavailable: {e}")

@app.post("/jackett/restart")
async def jackett_restart():
    """Restart Jackett — kill it and let watchdog auto-relaunch."""
    killed = _kill_jackett()
    if killed:
        import time; time.sleep(2)
    global JACKETT_API_KEY
    if not JACKETT_API_KEY:
        JACKETT_API_KEY = _jackett_api_key() or ""
    return {"status": "restarted", "killed": killed, "api_key_set": bool(JACKETT_API_KEY)}

# ── UFC Watch (background poller) ────────────────────────────
# Stores watch state in memory; persists across requests within process
_watches: dict = {}

@app.post("/watch/ufc")
async def watch_ufc(req: UfcWatchRequest):
    """Start watching for a UFC event torrent."""
    vpn_check()

    watch_key = req.event.lower().replace(" ", "_")

    if watch_key in _watches:
        return {"status": "already_watching", "event": req.event}

    _watches[watch_key] = {
        "event": req.event,
        "quality": req.quality,
        "found": False,
        "best_torrent": None,
        "polls": 0,
        "last_error": None,
    }

    # Start background poller
    asyncio.create_task(_poll_ufc(watch_key, req.event, req.quality, req.poll_interval))

    return {
        "status": "watching",
        "event": req.event,
        "quality": req.quality,
        "poll_interval": req.poll_interval,
    }

@app.get("/watch/ufc")
async def watch_ufc_status():
    """Get status of all UFC watches."""
    return {"watches": _watches}

@app.delete("/watch/ufc/{event_key}")
async def watch_ufc_stop(event_key: str):
    """Stop watching for a UFC event."""
    if event_key in _watches:
        del _watches[event_key]
        return {"status": "stopped", "event": event_key}
    return {"status": "not_found"}

async def _poll_ufc(key: str, event: str, quality: str, interval: int):
    """Background poller: search for UFC event torrents periodically."""
    while key in _watches and not _watches[key]["found"]:
        try:
            results = await _search_jackett(f"{event}", "all")
            if key not in _watches:
                break
            for r in results.get("results", []):
                title = r.get("title", "").lower()
                if event.lower().replace(" ", "") in title.replace(" ", ""):
                    # Check quality
                    if quality in title:
                        if not _watches[key]["best_torrent"] or \
                           r.get("seeders", 0) > _watches[key]["best_torrent"].get("seeders", 0):
                            _watches[key]["best_torrent"] = r
            _watches[key]["polls"] += 1
            _watches[key]["last_error"] = None
        except Exception as e:
            if key in _watches:
                _watches[key]["polls"] += 1
                _watches[key]["last_error"] = str(e)

        if key not in _watches:
            break
        if _watches[key]["best_torrent"] and _watches[key]["best_torrent"].get("seeders", 0) >= 2:
            _watches[key]["found"] = True
            break

        await asyncio.sleep(interval)


