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
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import quote

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

class JackettSearchRequest(BaseModel):
    query: str
    indexer: str = "all"  # "all" searches all configured indexers

class UfcWatchRequest(BaseModel):
    event: str  # e.g. "UFC 327"
    quality: str = "1080"  # preferred quality
    poll_interval: int = 300  # seconds between polls

# ── VPN helpers ──────────────────────────────────────────────
def _nordvpn(cmd: str) -> str:
    r = subprocess.run(
        f"nordvpn {cmd}", shell=True,
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
    key_param = f"apikey={JACKETT_API_KEY}" if JACKETT_API_KEY else ""
    sep = "&" if "?" in path else "?"
    return f"{base}{path}{sep}{key_param}"

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
        result = _nordvpn(f"connect {req.server}")
    else:
        result = _nordvpn(f"connect --group P2P '{req.country}'")
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
async def search_torrents_get(q: str, indexer: str = "all"):
    return await _search_jackett(q, indexer)

@app.post("/search/torrents")
async def search_torrents_post(req: JackettSearchRequest):
    return await _search_jackett(req.query, req.indexer)

async def _search_jackett(query: str, indexer: str = "all"):
    """Search torrents via Jackett Torznab API."""
    url = _jackett_url(
        f"/api/v2.0/indexers/{indexer}/results/torznab/api"
        f"?t=search&cat=2000&q={quote(query)}"
    )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"Jackett returned {resp.status_code}"
                )

            results = _parse_torznab(resp.text)
            return {
                "query": query,
                "indexer": indexer,
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

    for item in soup.select("item"):
        title = item.select_one("title")
        link = item.select_one("link")
        size = item.select_one("size")
        seeders = item.select_one("seeders")
        peers = item.select_one("peers")
        source = item.select_one("source") or item.select_one("jackettindexer")

        magnet = ""
        for attr in item.select("enclosure, link"):
            if attr.get("type") == "application/x-bittorrent" or \
               attr.get("url", "").startswith("magnet:"):
                magnet = attr.get("url", "")
                break

        # Also check torznab:attr for magnet
        for attr in item.select("attr"):
            if attr.get("name") == "magneturl":
                magnet = attr.get("value", "")
                break

        results.append({
            "title": title.get_text(strip=True) if title else "",
            "magnet": magnet,
            "size": int(size.get_text()) if size else 0,
            "seeders": int(seeders.get_text()) if seeders else 0,
            "peers": int(peers.get_text()) if peers else 0,
            "source": source.get_text(strip=True) if source else "",
            "link": link.get_text(strip=True) if link else "",
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
@app.post("/download/magnet")
async def download_magnet(req: TorrentMagnetRequest):
    """Download a torrent via aria2."""
    vpn_check()

    name_part = f" --out '{req.optional_name}'" if req.optional_name else ""

    # aria2c with sensible defaults
    cmd = (
        f"aria2c --seed-time=0 "
        f"--dir=/downloads "
        f"--summary-interval=10 "
        f"--max-connection-per-server=4 "
        f"--split=4 "
        f"{name_part} "
        f"'{req.magnet}'"
    )

    r = subprocess.Popen(
        cmd, shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    return {
        "status": "started",
        "magnet": req.magnet[:80] + "...",
        "output_dir": "/downloads",
        "pid": r.pid,
    }

# ── Download management ─────────────────────────────────────
@app.get("/downloads/active")
async def active_downloads():
    """List running aria2 processes."""
    r = subprocess.run(
        ["pgrep", "-a", "aria2c"],
        capture_output=True, text=True
    )
    return {"processes": r.stdout.strip() or "none"}

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
    url = _jackett_url("/api/v2.0/indexers")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            return resp.json()
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
            for r in results.get("results", []):
                title = r.get("title", "").lower()
                if event.lower().replace(" ", "") in title.replace(" ", ""):
                    # Check quality
                    if quality in title:
                        if not _watches[key]["best_torrent"] or \
                           r.get("seeders", 0) > _watches[key]["best_torrent"].get("seeders", 0):
                            _watches[key]["best_torrent"] = r
            _watches[key]["polls"] += 1
        except Exception:
            pass

        if _watches[key]["best_torrent"] and _watches[key]["best_torrent"].get("seeders", 0) >= 2:
            _watches[key]["found"] = True
            break

        await asyncio.sleep(interval)
