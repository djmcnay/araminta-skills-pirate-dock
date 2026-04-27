"""
Browser fallback via Playwright (Chromium runs INSIDE the container).

All traffic routes through NordVPN automatically because the browser
launches within the container's network namespace.

When headless scraping hits a CAPTCHA or DDoS-Guard visual challenge:
  A) A screenshot is taken and returned as base64 — the calling agent
     can use its own vision model to decide what to do.
  B) The noVNC URL is returned so the user can view/interact via browser.
  C) The browser waits in headed mode (DISPLAY=:1) for the human to solve.
  D) Once the page advances, download extraction resumes automatically.

The container does NOT call any external LLM/vision API. That logic
belongs in the calling agent (Minty), not the container.
"""

import os
import base64
import json
import asyncio
from pathlib import Path
import warnings

DOWNLOAD_DIR = Path("/downloads")

# Anna's Archive mirrors (tried in order; .li is DEAD — see skill notes)
ANNAS_MIRRORS = [
    "https://annas-archive.gl",
    "https://annas-archive.pk",
    "https://annas-archive.gd",
]

# Stealth init script — injected into every page
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = window.chrome || {};
window.chrome.runtime = {};
Object.defineProperty(navigator, 'languages', {get: () => ['en-GB', 'en']});
"""


def _check_playwright():
    try:
        from playwright.async_api import async_playwright
        return True
    except ImportError:
        return False


async def _launch_browser(p, headless=True):
    """Launch Chromium with stealth args."""
    args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--window-size=1280,800",
    ]
    if not headless:
        display = os.environ.get("DISPLAY")
        if not display:
            warnings.warn("headless=False but DISPLAY not set — browser may fail to start")
    browser = await p.chromium.launch(headless=headless, args=args)
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        locale="en-GB",
        user_agent=(
            "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
        ),
    )
    page = await context.new_page()
    await page.add_init_script(_STEALTH_JS)
    return browser, page


async def _detect_challenge(page):
    """Detect what kind of challenge (if any) is on the current page."""
    title = await page.title()
    url = page.url
    body = ""
    try:
        body = await page.evaluate("() => document.body.innerText")
    except Exception:
        pass

    body_lower = body.lower()

    # DDoS-Guard
    title_lower = title.lower()
    if "ddos-guard" in title_lower or "checking your browser" in title_lower:
        if "manual check" in body_lower or "complete the manual" in body_lower:
            return {"challenge": "ddos_guard_manual", "title": title, "url": url}
        return {"challenge": "ddos_guard_js", "title": title, "url": url}
    # Body text alone is not enough — "checking your browser" appears in AA's FAQ.
    # Only flag if title is absent or also DDoS-Guard-like.
    if ("ddos-guard" in body_lower or "checking your browser" in body_lower) and \
       (not title.strip() or title_lower.startswith("checking") or title_lower.startswith("ddos")):
        if "manual check" in body_lower or "complete the manual" in body_lower:
            return {"challenge": "ddos_guard_manual", "title": title, "url": url}
        return {"challenge": "ddos_guard_js", "title": title, "url": url}

    # hCaptcha / reCAPTCHA
    captcha_frames = await page.locator(
        "iframe[src*='hcaptcha'], iframe[src*='recaptcha'], "
        "div.h-captcha, div.g-recaptcha"
    ).count()
    if captcha_frames > 0:
        return {"challenge": "captcha", "type": "visual", "title": title, "url": url}

    # Cloudflare
    if any(x in body_lower for x in ["just a moment", "verifying", "are you human"]):
        return {"challenge": "cloudflare", "title": title, "url": url}

    # Check for countdown / download ready
    countdown = await page.locator("text=/countdown|seconds|please wait/i").count()
    if countdown > 0:
        return {"challenge": "countdown", "title": title, "url": url}

    # Check for actual download links
    links = await page.evaluate("""
        JSON.stringify(
            Array.from(document.querySelectorAll('a[href]'))
                .filter(a => {
                    const t = (a.textContent || '').toLowerCase();
                    return t.includes('download') || t.includes('get file')
                           || a.href.includes('.pdf') || a.href.includes('.epub');
                })
                .map(a => ({
                    text: (a.textContent || '').trim().substring(0,80),
                    url: a.href
                }))
        )
    """)
    parsed = json.loads(links)
    if parsed:
        return {"challenge": "none", "download_links": parsed, "title": title, "url": url}

    return {"challenge": "none", "title": title, "url": url}


def _screenshot_b64(screenshot_path) -> str | None:
    """Read a screenshot file and return base64-encoded PNG, or None on error."""
    try:
        with open(screenshot_path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except Exception:
        return None


async def _handle_countdown_and_extract(page, timeout=180) -> dict:
    """
    Wait for countdown to finish, extract the token download URL,
    then curl it to /downloads. Returns dict with status/file_path.
    """
    import time
    from urllib.parse import unquote

    start = time.time()
    token_url = None

    while time.time() - start < timeout:
        await asyncio.sleep(3)

        # 1. Look for visible hrefs matching token patterns
        js_find = """
        JSON.stringify(
            Array.from(document.querySelectorAll('a[href], button[data-clipboard-text], input[value], textarea'))
                .map(el => ({
                    text: (el.textContent || '').trim().substring(0,60),
                    href: el.href || el.dataset.clipboardText || el.value || el.textContent || ''
                }))
                .filter(o => o.href.includes('wbsg8v') || o.href.includes('/d3/y/')
                          || o.href.includes('.epub') || o.href.includes('.pdf')
                          || o.href.includes('.zip') || o.href.includes('.mobi'))
        )
        """
        try:
            found = await page.evaluate(js_find)
            parsed = json.loads(found)
            if parsed:
                token_url = parsed[0].get("href", "")
                if token_url:
                    break
        except Exception:
            pass

        # 2. Try the Copy button's clipboard data
        if not token_url:
            try:
                btn = page.locator("button:has-text('Copy'), button:has-text('copy')")
                if await btn.count() > 0:
                    href = await btn.first.get_attribute("data-clipboard-text")
                    if href:
                        token_url = href
                        break
            except Exception:
                pass

        # 3. Check if page URL itself is now the token (redirected)
        current = page.url
        if "wbsg8v" in current or "/d3/y/" in current:
            token_url = current
            break

    if not token_url:
        return {"status": "timeout", "message": "Countdown ended but no token URL found"}

    # Extract filename
    filename = unquote(token_url.split('/')[-1].split('?')[0])
    if not filename or len(filename) < 4:
        filename = f"anna_{int(time.time())}.epub"
    output_path = DOWNLOAD_DIR / filename

    # Grab cookies for curl
    cookies = await page.context.cookies()
    cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])

    cmd = [
        "curl", "-L", "-s", "-o", str(output_path),
        "-H", f"Cookie: {cookie_str}",
        "-H", "User-Agent: Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
        "-H", f"Referer: {page.url}",
        token_url
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode == 0 and output_path.exists() and output_path.stat().st_size > 1024:
        return {
            "status": "success",
            "message": f"Downloaded {filename} ({output_path.stat().st_size} bytes)",
            "file_path": str(output_path),
            "token_url": token_url,
        }
    else:
        return {
            "status": "error",
            "message": f"curl failed: {stderr.decode()[:200]}",
            "token_url": token_url,
        }


async def browser_download(
    md5: str,
    mirror: str | None = None,
    wait_for_human: int = 120,
    headless: bool = True,
) -> dict:
    """
    Navigate Anna's Archive, click Slow Partner Server, handle DDoS-Guard/CAPTCHA.

    Returns dict with keys:
      - status: "success" | "captcha_required" | "challenge_js" | "timeout" | "error"
      - download_links: list of {text, url} (only on success)
      - message: human-readable explanation
      - page_url, mirror, md5
    """
    if not _check_playwright():
        return {
            "status": "error",
            "message": "Playwright not installed. Run: pip install playwright && playwright install chromium",
            "md5": md5,
        }

    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

    mirror_base = mirror or ANNAS_MIRRORS[0]
    page_url = f"{mirror_base}/md5/{md5}"

    result = {
        "md5": md5,
        "page_url": page_url,
        "mirror": mirror_base,
        "method": "playwright",
    }

    async with async_playwright() as p:
        browser = None
        try:
            browser, page = await _launch_browser(p, headless=headless)

            # ── Step 1: Navigate to book page ──
            await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            # ── Step 2: Look for download links / buttons ──
            download_keywords = [
                "partner", "slow partner", "download", "torrent",
                "get file", "fast download", "free download", "external download",
            ]
            all_links = await page.locator("a, button").all()
            scored = []
            for link in all_links:
                try:
                    if not await link.is_visible():
                        continue
                except Exception:
                    continue
                txt = (await link.text_content() or "").strip().lower()
                href = (await link.get_attribute("href")) or ""
                score = 0
                for i, kw in enumerate(download_keywords):
                    if kw in txt:
                        score += len(download_keywords) - i
                if "partner" in txt:
                    score += 50
                if href and ("/db/" in href or href.endswith((".epub", ".pdf", ".zip", ".mobi"))):
                    score += 5
                if score:
                    scored.append((score, link))
            scored.sort(reverse=True, key=lambda x: x[0])
            slow_links = [lnk for _, lnk in scored]

            if not slow_links:
                novnc_url = os.environ.get("NOVNC_URL", "http://100.65.212.67:5998/vnc.html")
                result["status"] = "no_links_found"
                result["message"] = (
                    f"No download links found on book page. "
                    f"Open the browser to inspect: {novnc_url}"
                )
                return result

            # Click the best candidate with a short timeout
            clicked = False
            for link in slow_links:
                try:
                    await link.click(timeout=5000)
                    clicked = True
                    break
                except Exception:
                    continue
            if not clicked:
                result["status"] = "error"
                result["message"] = "Found download links but none were clickable."
                return result
            await asyncio.sleep(2)

            # ── Step 3: Handle DDoS-Guard / challenge loop ──
            start_time = asyncio.get_event_loop().time()
            while True:
                await asyncio.sleep(2)
                state = await _detect_challenge(page)

                if state["challenge"] == "none" and state.get("download_links"):
                    result["status"] = "success"
                    result["download_links"] = state["download_links"]
                    result["message"] = "Download links found after challenge resolved."
                    return result

                if state["challenge"] == "ddos_guard_js":
                    # Just wait — JS will auto-redirect
                    elapsed = asyncio.get_event_loop().time() - start_time
                    if elapsed > 30:
                        result["status"] = "timeout"
                        result["message"] = "DDoS-Guard JS challenge did not auto-resolve within 30s."
                        return result
                    continue

                if state["challenge"] in ("ddos_guard_manual", "captcha"):
                    # ── Visual challenge — take screenshot, return to calling agent ─
                    # The calling agent (Minty) uses its own vision model if available.
                    # If not, it sends the noVNC URL to the user for manual solving.
                    novnc_url = os.environ.get("NOVNC_URL", "http://100.65.212.67:5998/vnc.html")
                    screenshot_path = DOWNLOAD_DIR / f"captcha_{md5}.png"
                    screenshot_b64 = None
                    try:
                        await page.screenshot(path=str(screenshot_path))
                        screenshot_b64 = _screenshot_b64(str(screenshot_path))
                    except Exception:
                        screenshot_path = None

                    result["status"] = "captcha_required"
                    result["novnc_url"] = novnc_url
                    result["message"] = (
                        f"Visual CAPTCHA/DDoS-Guard challenge detected. "
                        f"Open the browser to solve it: {novnc_url} — "
                        f"I'll wait up to {wait_for_human}s."
                    )
                    if screenshot_path and Path(screenshot_path).exists():
                        result["screenshot_path"] = str(screenshot_path)
                    if screenshot_b64:
                        result["screenshot_b64"] = screenshot_b64

                    # Wait for page URL to change (human solves via noVNC)
                    previous_url = page.url
                    for _ in range(wait_for_human // 2):
                        await asyncio.sleep(2)
                        if page.url != previous_url:
                            state2 = await _detect_challenge(page)
                            if state2.get("download_links"):
                                result["status"] = "success"
                                result["download_links"] = state2["download_links"]
                                result["message"] = "Download links found after CAPTCHA solve."
                                result.pop("screenshot_b64", None)
                                return result
                            if state2["challenge"] == "countdown":
                                break

                    result["status"] = "timeout"
                    result["message"] = f"Timed out after {wait_for_human}s waiting for CAPTCHA solve."
                    return result

                if state["challenge"] == "countdown":
                    # Countdown page — wait for it, extract token URL, curl file
                    countdown_result = await _handle_countdown_and_extract(page)
                    if countdown_result["status"] == "success":
                        result["status"] = "success"
                        result["download_links"] = [
                            {"text": "direct", "url": countdown_result["token_url"]}
                        ]
                        result["file_path"] = countdown_result["file_path"]
                        result["message"] = countdown_result["message"]
                        return result
                    # If extraction failed, keep going (might still be counting)
                    await asyncio.sleep(10)
                    continue

                # Unknown state
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > wait_for_human:
                    result["status"] = "timeout"
                    result["message"] = "Timed out waiting for challenge to resolve."
                    return result

        except PlaywrightTimeout:
            result["status"] = "error"
            result["message"] = "Playwright timeout — page did not load."
            return result
        except Exception as e:
            result["status"] = "error"
            result["message"] = f"Browser error: {e}"
            return result
        finally:
            if browser:
                await browser.close()


async def browser_status() -> dict:
    """Check if browser stack is available."""
    if not _check_playwright():
        return {"available": False, "reason": "playwright not installed"}
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            await browser.close()
            return {"available": True, "type": "local_chromium", "via": "container"}
    except Exception as e:
        return {"available": False, "reason": str(e)}


# ── Back-compat aliases for old server.py imports ─────────────────────────

class CDPError(Exception):
    """Old CDP error class — kept for import compatibility."""
    pass


class BrowserFallback:
    """
    Shim for old server.py code that instantiates BrowserFallback().
    Redirects all calls to the new Playwright-based implementation.
    """
    def __init__(self):
        warnings.warn(
            "BrowserFallback(CDP) is deprecated — Playwright local Chromium is used now",
            stacklevel=2,
        )

    async def connect(self):
        """No-op — local browser needs no connect."""
        pass

    async def disconnect(self):
        """No-op."""
        pass

    async def navigate(self, url: str, wait: float = 3.0):
        """Stub — not used by new server flow."""
        pass

    async def get_page_content(self):
        """Stub."""
        return ""

    async def wait_for_download_link(self, timeout=120, poll_interval=2.0):
        """Stub — new flow uses browser_download() directly."""
        return []

    async def screenshot_b64(self):
        """Stub."""
        return ""


# Also expose old name
cdp_status = browser_status
