"""
Orchestrated download endpoint for pirate-dock.

Exposes a single endpoint that handles the complete Anna's Archive flow:
  POST /download/annas-archive/orchestrate

Two call patterns:
  1. Fresh start: {"md5": "..."}  
     → Navigates book page, clicks Slow Partner #1, handles DDoS → hCaptcha
     → Returns captcha_required + VNC URL, or success if no captcha

  2. Resume after captcha: {"md5": "...", "resume": true}
     → Reconnects to existing browser page on the slow_download URL
     → Polls for page change (David solves captcha via VNC)
     → Handles countdown → finds token URL → curls file → returns success
"""

import os
import json
import asyncio
import base64
import re
from pathlib import Path
from urllib.parse import unquote

DOWNLOAD_DIR = Path("/downloads")
DISPLAY_URL = os.environ.get(
    "DISPLAY_URL",
    "https://araminta.taild3f7b9.ts.net/pirate/vnc_lite.html?path=pirate%2F",
)
CDP_PORT = int(os.environ.get("CDP_PORT", "9223"))
ANNAS_MIRRORS = ["https://annas-archive.gl", "https://annas-archive.pk", "https://annas-archive.gd"]

# ── Shared safety helpers ────────────────────────────────────

def safe_download_filename(token_url: str, md5: str) -> str:
    """Derive a strictly-safe basename from a token URL.

    Defence against URL-encoded path traversal (e.g. %2Fapp%2Fserver.py):
    take the last path segment, URL-decode it, strip anything that is not a
    safe filename character, and fall back to a generated name.
    """
    raw = unquote(token_url.split("/")[-1].split("?")[0])
    raw = raw.replace("\\", "/").split("/")[-1]          # no separators survive
    raw = re.sub(r"[^A-Za-z0-9._ ()'&-]", "", raw).strip(". ")  # allowlist charset
    if len(raw) < 4 or raw in {".", ".."}:
        raw = f"anna_{md5[:8]}.epub"
    if len(raw) > 120:
        name, ext = os.path.splitext(raw)
        raw = f"{name[:80]}{ext}"
    return raw


def build_scoped_cookie_header(cookies: list, origin_host: str) -> str:
    """Only send cookies whose domain matches the download origin (+ parent domains)."""
    host = origin_host.lower()
    keep = []
    for c in cookies:
        d = (c.get("domain") or "").lower().lstrip(".")
        if d and (host == d or host.endswith("." + d)):
            keep.append(f"{c['name']}={c['value']}")
    return "; ".join(keep)


def _check_playwright() -> bool:
    try:
        from playwright.async_api import async_playwright  # noqa: F401
        return True
    except ImportError:
        return False


async def _connect_to_persistent(p, target_url_hint: str | None = None):
    """Connect to persistent Chromium, reusing a relevant page if possible."""
    cdp_url = f"http://127.0.0.1:{CDP_PORT}"
    browser = await p.chromium.connect_over_cdp(cdp_url)

    page = None
    contexts = browser.contexts
    if contexts:
        context = contexts[0]
        # Look for existing page on slow_download or captcha
        for existing in context.pages:
            u = existing.url
            if u and ("slow_download" in u or "captcha" in u.lower() 
                      or "checking" in u.lower() or "ddos" in u.lower()
                      or "hcaptcha" in u.lower()):
                page = existing
                break
        # If hint matches an existing page
        if not page and target_url_hint:
            for existing in context.pages:
                if target_url_hint in existing.url:
                    page = existing
                    break
        # Fallback to newest page that isn't about:blank
        if not page:
            for existing in reversed(context.pages):
                if existing.url and existing.url != "about:blank":
                    page = existing
                    break
        # Last resort: new page
        if not page:
            page = await context.new_page()

    if not page:
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="en-GB",
            timezone_id="Africa/Johannesburg",
            user_agent="Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

    return browser, page


async def _detect_state(page) -> dict:
    """Detect page state. Captcha check FIRST."""
    try:
        title = await page.title()
    except Exception:
        title = ""
    url = page.url

    body = ""
    try:
        body = await page.evaluate("() => document.body?.innerText || ''")
    except Exception:
        pass
    body_lower = body.lower()

    # Captcha — check first
    try:
        captcha_count = await page.locator(
            "iframe[src*='hcaptcha'], iframe[src*='recaptcha']"
        ).count()
    except Exception:
        captcha_count = 0
    if captcha_count > 0:
        return {"state": "captcha_visual", "title": title, "url": url}

    # DDoS-Guard
    title_lower = title.lower()
    if "ddos-guard" in title_lower or "checking your browser" in title_lower:
        if "manual check" in body_lower:
            return {"state": "ddos_guard_manual", "title": title, "url": url}
        return {"state": "ddos_guard_js", "title": title, "url": url}

    # Countdown
    if any(x in body_lower for x in ["countdown", "seconds until", "minutes until", "please wait"]):
        return {"state": "countdown", "title": title, "url": url}

    # Token URL in current URL
    if "wbsg8v" in url or "/d3/y/" in url:
        return {"state": "token_url_found", "title": title, "url": url}

    # Find token URLs in DOM
    try:
        found = await page.evaluate("""
            JSON.stringify(
                Array.from(document.querySelectorAll('a[href], input[value], textarea'))
                    .map(el => el.href || el.value || el.textContent || '')
                    .filter(s => s.startsWith('http') && (s.includes('wbsg8v') || s.includes('/d3/y/')))
            )
        """)
        urls = json.loads(found)
        if urls:
            return {"state": "token_url_found", "token_url": urls[0], "title": title, "url": url}
    except Exception:
        pass

    # Download links (Slow/Fast Partner Servers)
    try:
        links_raw = await page.evaluate("""
            JSON.stringify(
                Array.from(document.querySelectorAll('a[href]'))
                    .filter(a => {
                        const t = (a.textContent || '').toLowerCase();
                        const h = a.href || '';
                        return t.includes('slow download') || t.includes('fast download')
                            || t.includes('partner server') || h.includes('/slow_download/')
                            || h.includes('/fast_download/');
                    })
                    .map(a => ({text: (a.textContent||'').trim().substring(0,100), url: a.href}))
            )
        """)
        links = json.loads(links_raw)
        if links:
            return {"state": "download_ready", "download_links": links, "title": title, "url": url}
    except Exception:
        pass

    return {"state": "other", "title": title, "url": url}


async def orchestrate_download(
    md5: str,
    mirror: str | None = None,
    resume: bool = False,
) -> dict:
    """
    Complete end-to-end download orchestration.

    Fresh start: navigates book page → clicks Slow Partner → handles chain.
    Resume: reconnects to existing captcha page, polls for solve → countdown → download.
    """
    if not _check_playwright():
        return {"status": "error", "message": "Playwright not installed", "md5": md5}

    from playwright.async_api import async_playwright

    mirror_base = mirror or ANNAS_MIRRORS[0]
    book_url = f"{mirror_base}/md5/{md5}"
    slow_url = f"{mirror_base}/slow_download/{md5}/0/0"

    result = {"md5": md5, "mirror": mirror_base, "method": "cdp_connect"}

    async with async_playwright() as p:
        try:
            if resume:
                # ── RESUME: navigate to slow_download page, poll for solve ──
                browser, page = await _connect_to_persistent(p)
                
                await page.goto(slow_url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(4)
                
                result["phase"] = "resume"
                result["current_url"] = page.url
                
                # Poll for page change (captcha solved → countdown → token)
                last_url = page.url
                for i in range(90):  # 180 seconds
                    await asyncio.sleep(2)
                    state = await _detect_state(page)
                    
                    if state["state"] == "token_url_found":
                        result["phase"] = "token_url_found"
                        break
                    elif state["state"] == "countdown":
                        result["phase"] = "countdown"
                        result["message"] = "Countdown detected, waiting for download..."
                        continue
                    elif state["state"] in ("captcha_visual", "ddos_guard_js", "ddos_guard_manual"):
                        if i < 5:
                            result["phase"] = "still_captcha"
                        continue
                    else:
                        if page.url != last_url:
                            last_url = page.url
                            result["current_url"] = page.url
                
                # After poll loop, check final state
                state = await _detect_state(page)
                result["waited_seconds"] = (result.get("waited_seconds", 0) + 180)
                result["current_url"] = page.url

            else:
                # ── FRESH START: full flow from book page ──
                browser, page = await _connect_to_persistent(p)
                
                # Phase 1: Navigate to book page
                await page.goto(book_url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(4)
                state = await _detect_state(page)
                result["phase"] = "book_page"
                
                # Phase 2: If download links visible, click Slow Partner #1
                if state["state"] == "download_ready":
                    dl_links = state.get("download_links", [])
                    slow_url_from_page = None
                    for dl in dl_links:
                        if "slow partner server" in dl.get("text", "").lower():
                            slow_url_from_page = dl["url"]
                            break
                    
                    if slow_url_from_page:
                        try:
                            await page.goto(slow_url_from_page, wait_until="domcontentloaded", timeout=30000)
                        except Exception:
                            pass
                        await asyncio.sleep(5)
                        state = await _detect_state(page)
                        result["phase"] = "slow_download"
                
                # Phase 3: If captcha, return to caller
                if state["state"] in ("captcha_visual", "ddos_guard_js", "ddos_guard_manual"):
                    screenshot_path = DOWNLOAD_DIR / f"orchestrate_{md5}.png"
                    await page.screenshot(path=str(screenshot_path))
                    return {
                        **result,
                        "status": "captcha_required",
                        "state": state["state"],
                        "message": f"Captcha needs solving. David: {DISPLAY_URL}",
                        "display_url": DISPLAY_URL,
                        "screenshot_path": str(screenshot_path),
                        "current_url": page.url,
                    }

            # ── Phase 4: Token URL found — download it ──
            token_url = state.get("token_url") or (state.get("url", "") if "wbsg8v" in state.get("url", "") or "/d3/y/" in state.get("url", "") else None)
            
            if not token_url and state["state"] != "token_url_found":
                # Try one more scan
                try:
                    found = await page.evaluate("""
                        JSON.stringify(
                            Array.from(document.querySelectorAll('a[href]'))
                                .map(a => a.href)
                                .filter(h => h.includes('wbsg8v') || h.includes('/d3/y/'))
                        )
                    """)
                    urls = json.loads(found)
                    if urls:
                        token_url = urls[0]
                except Exception:
                    pass

            if not token_url:
                screenshot_path = DOWNLOAD_DIR / f"orchestrate_stuck_{md5}.png"
                await page.screenshot(path=str(screenshot_path))
                return {
                    **result,
                    "status": "timeout",
                    "state": "no_token",
                    "message": f"No download URL found. Last URL: {page.url[:120]}",
                    "display_url": DISPLAY_URL,
                    "screenshot_path": str(screenshot_path),
                }

            # ── Phase 5: Curl the file ──
            filename = safe_download_filename(token_url, md5)
            output_path = DOWNLOAD_DIR / filename
            # Validate MD5 before it reaches any file path
            if not re.fullmatch(r"[a-f0-9]{32}", md5):
                return {**result, "status": "error", "state": "bad_md5",
                        "message": "md5 must be 32 hex chars"}
            part_path = DOWNLOAD_DIR / f".part_{filename}"
            if part_path.exists():
                part_path.unlink()

            cookies = await page.context.cookies()
            origin_host = re.sub(r"^https?://", "", mirror_base).split("/")[0]
            cookie_str = build_scoped_cookie_header(cookies, origin_host)

            proc = await asyncio.create_subprocess_exec(
                "curl", "-L", "-sS", "-o", str(part_path),
                "-w", "%{http_code} %{size_download}",
                "-H", f"Cookie: {cookie_str}",
                "-H", "User-Agent: Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
                "-H", f"Referer: {page.url}",
                "--fail-with-body",
                "--max-time", "300",
                token_url,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            http_code = (stdout.decode().split() or ["0"])[0] if stdout else "0"

            if (proc.returncode == 0 and http_code == "200"
                    and part_path.exists() and part_path.stat().st_size > 1024):
                # Sanity-check content: a challenge page would be HTML, not the real file
                head = part_path.open("rb").read(512).lower()
                if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
                    part_path.unlink(missing_ok=True)
                    return {
                        **result,
                        "status": "error",
                        "state": "html_not_file",
                        "message": "Download URL returned an HTML page (challenge/error), not the file.",
                        "token_url": token_url,
                    }
                # Atomic publish: final name only ever holds a verified file
                if output_path.exists():
                    output_path.unlink()
                part_path.rename(output_path)
                return {
                    **result,
                    "status": "success",
                    "state": "downloaded",
                    "message": f"Downloaded {filename} ({output_path.stat().st_size:,} bytes)",
                    "file_path": str(output_path),
                    "file_size": output_path.stat().st_size,
                    "token_url": token_url,
                }

            part_path.unlink(missing_ok=True)
            return {
                **result,
                "status": "error",
                "state": "curl_failed",
                "message": f"curl failed (exit {proc.returncode}, http {http_code})",
                "token_url": token_url,
            }

        except Exception as e:
            return {"status": "error", "state": "exception", "message": f"Orchestration failed: {e}", "md5": md5}
