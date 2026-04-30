"""
Browser fallback for pirate-dock — CDP-driven, minimal.

Architecture:
  run.sh launches Chromium headed on :1 with --remote-debugging-port=9223.
  This module provides three functions that Minty drives via CDP:
    navigate(md5)  → opens book page, returns screenshot + state
    wait_for_page_change(timeout) → polls URL, returns when page advances
    extract_download() → finds download links, curls file to /downloads

No Camoufox. No hCaptcha auto-click. No button-hunting heuristics.
Visual puzzles go to David via the display URL. Minty drives everything else.

CDP endpoint: ws://127.0.0.1:9223/devtools/browser/{id}
"""

import os
import json
import asyncio
import base64
from pathlib import Path
from urllib.parse import unquote
import warnings

DOWNLOAD_DIR = Path("/downloads")
DISPLAY_URL = os.environ.get(
    "DISPLAY_URL",
    "https://araminta.taild3f7b9.ts.net/pirate/vnc_lite.html?path=pirate%2F",
)
CDP_PORT = int(os.environ.get("CDP_PORT", "9223"))

ANNAS_MIRRORS = [
    "https://annas-archive.gl",
    "https://annas-archive.pk",
    "https://annas-archive.gd",
]


def _check_playwright() -> bool:
    """Check if Playwright is importable."""
    try:
        from playwright.async_api import async_playwright  # noqa: F401
        return True
    except ImportError:
        return False


async def _launch_browser(p, headless: bool = False):
    """Launch Chromium via Playwright. Returns (browser, page)."""
    args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--window-size=1280,800",
        f"--remote-debugging-port={CDP_PORT}",
    ]
    if not headless and not os.environ.get("DISPLAY"):
        warnings.warn("headless=False but DISPLAY not set — browser may fail")

    browser = await p.chromium.launch(headless=headless, args=args)
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        locale="en-GB",
        timezone_id="Africa/Johannesburg",
        user_agent=(
            "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
        ),
    )
    page = await context.new_page()

    # Stealth init
    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        window.chrome = window.chrome || {};
        window.chrome.runtime = {};
    """)

    return browser, page


async def _detect_state(page) -> dict:
    """Detect what's on the current page. Returns state dict."""
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

    # DDoS-Guard
    title_lower = title.lower()
    if "ddos-guard" in title_lower or "checking your browser" in title_lower:
        if "manual check" in body_lower:
            return {"state": "ddos_guard_manual", "title": title, "url": url}
        return {"state": "ddos_guard_js", "title": title, "url": url}

    # hCaptcha frames
    try:
        captcha_count = await page.locator(
            "iframe[src*='hcaptcha'], iframe[src*='recaptcha']"
        ).count()
    except Exception:
        captcha_count = 0
    if captcha_count > 0:
        return {"state": "captcha_visual", "title": title, "url": url}

    # Cloudflare
    if any(x in body_lower for x in ["just a moment", "verifying you are human"]):
        return {"state": "cloudflare", "title": title, "url": url}

    # Countdown
    if any(x in body_lower for x in ["countdown", "please wait", "seconds"]):
        return {"state": "countdown", "title": title, "url": url}

    # Download links
    try:
        links_raw = await page.evaluate("""
            JSON.stringify(
                Array.from(document.querySelectorAll('a[href]'))
                    .filter(a => {
                        const t = (a.textContent || '').toLowerCase();
                        return t.includes('download') || t.includes('get file')
                            || a.href.includes('.pdf') || a.href.includes('.epub')
                            || a.href.includes('.mobi') || a.href.includes('.zip')
                            || a.href.includes('wbsg8v') || a.href.includes('/d3/y/');
                    })
                    .map(a => ({text: (a.textContent||'').trim().substring(0,100), url: a.href}))
            )
        """)
        links = json.loads(links_raw)
        if links:
            return {"state": "download_ready", "download_links": links, "title": title, "url": url}
    except Exception:
        pass

    return {"state": "unknown", "title": title, "url": url}


def _screenshot_b64(path_str: str) -> str | None:
    """Read a PNG screenshot and return base64 string."""
    try:
        with open(path_str, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except Exception:
        return None


# ── Public API ──────────────────────────────────────────────────

async def browser_navigate(
    md5: str,
    mirror: str | None = None,
    headless: bool = False,
) -> dict:
    """
    Navigate to an Anna's Archive book page and return its state.

    Launches Chromium, goes to the MD5 page, takes a screenshot,
    detects what's on the page, returns everything the caller needs.

    Returns dict with:
      - status: "ok" | "error"
      - state: one of the state strings from _detect_state
      - page_url, mirror, md5
      - screenshot_path (local path to PNG)
      - screenshot_b64 (base64-encoded PNG)
      - display_url (VNC link for human-in-the-loop)
      - cdp_port (for CDP control)
      - download_links (if state == "download_ready")
    """
    if not _check_playwright():
        return {
            "status": "error",
            "message": "Playwright not installed",
            "md5": md5,
        }

    from playwright.async_api import async_playwright

    mirror_base = mirror or ANNAS_MIRRORS[0]
    page_url = f"{mirror_base}/md5/{md5}"

    result = {
        "md5": md5,
        "page_url": page_url,
        "mirror": mirror_base,
        "display_url": DISPLAY_URL,
        "cdp_port": CDP_PORT,
    }

    async with async_playwright() as p:
        browser = None
        try:
            browser, page = await _launch_browser(p, headless=headless)
            result["method"] = "playwright_chromium"

            # Navigate
            await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)  # Let JS challenges fire

            # Screenshot
            screenshot_path = DOWNLOAD_DIR / f"navigate_{md5}.png"
            await page.screenshot(path=str(screenshot_path))
            result["screenshot_path"] = str(screenshot_path)
            screenshot_b64 = _screenshot_b64(str(screenshot_path))
            if screenshot_b64:
                result["screenshot_b64"] = screenshot_b64

            # Detect state
            state = await _detect_state(page)
            result["state"] = state["state"]
            result["page_title"] = state.get("title", "")
            if state.get("download_links"):
                result["download_links"] = state["download_links"]

            result["status"] = "ok"
            if state["state"] == "captcha_visual":
                result["message"] = (
                    f"hCaptcha detected. David needs to solve it at: {DISPLAY_URL}"
                )
            elif state["state"] == "ddos_guard_js":
                result["message"] = (
                    "DDoS-Guard JS challenge — waiting for auto-redirect "
                    "(may resolve silently or escalate to manual check)"
                )
            elif state["state"] == "ddos_guard_manual":
                result["message"] = (
                    f"DDoS-Guard manual check. David needs to interact at: {DISPLAY_URL}"
                )
            elif state["state"] == "download_ready":
                result["message"] = "Download links found on page."
            else:
                result["message"] = f"Page loaded, state: {state['state']}"

            return result

        except Exception as e:
            result["status"] = "error"
            result["message"] = f"Navigation failed: {e}"
            return result
        finally:
            if browser:
                await browser.close()


async def browser_wait_for_change(
    md5: str,
    mirror: str | None = None,
    timeout: int = 120,
    headless: bool = False,
) -> dict:
    """
    Re-navigate to the book page and wait for it to change state.

    After David solves a CAPTCHA, the page will redirect/change.
    This polls the URL until it differs from the initial page,
    then returns the new state.

    Use case: call after browser_navigate returns captcha_visual.
    David solves via VNC, then this detects the transition.

    Returns same shape as browser_navigate, plus:
      - waited_seconds: how long the poll ran
    """
    if not _check_playwright():
        return {"status": "error", "message": "Playwright not installed", "md5": md5}

    from playwright.async_api import async_playwright

    mirror_base = mirror or ANNAS_MIRRORS[0]
    page_url = f"{mirror_base}/md5/{md5}"

    result = {
        "md5": md5,
        "page_url": page_url,
        "mirror": mirror_base,
        "display_url": DISPLAY_URL,
        "cdp_port": CDP_PORT,
    }

    async with async_playwright() as p:
        browser = None
        try:
            browser, page = await _launch_browser(p, headless=headless)

            await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            initial_url = page.url
            start_time = asyncio.get_event_loop().time()

            for _ in range(timeout // 2):
                await asyncio.sleep(2)
                current_url = page.url
                if current_url != initial_url and current_url != "about:blank":
                    break
                # Also check page content for countdown/links even if URL unchanged
                state = await _detect_state(page)
                if state["state"] in ("countdown", "download_ready"):
                    break

            elapsed = asyncio.get_event_loop().time() - start_time
            result["waited_seconds"] = round(elapsed, 1)
            result["initial_url"] = initial_url
            result["final_url"] = page.url

            # Screenshot
            screenshot_path = DOWNLOAD_DIR / f"waited_{md5}.png"
            await page.screenshot(path=str(screenshot_path))
            result["screenshot_path"] = str(screenshot_path)
            screenshot_b64 = _screenshot_b64(str(screenshot_path))
            if screenshot_b64:
                result["screenshot_b64"] = screenshot_b64

            state = await _detect_state(page)
            result["state"] = state["state"]
            result["page_title"] = state.get("title", "")
            if state.get("download_links"):
                result["download_links"] = state["download_links"]

            if result["final_url"] == initial_url and state["state"] not in (
                "countdown",
                "download_ready",
            ):
                result["status"] = "timeout"
                result["message"] = (
                    f"Page did not change after {elapsed}s. "
                    f"David may not have solved the CAPTCHA yet, "
                    f"or the page is stuck. Check: {DISPLAY_URL}"
                )
            elif state["state"] == "download_ready":
                result["status"] = "ok"
                result["message"] = "Download links found after waiting."
            elif state["state"] == "countdown":
                result["status"] = "ok"
                result["message"] = "Countdown page detected — extracting download..."
            else:
                result["status"] = "ok"
                result["message"] = f"Page changed, state: {state['state']}"

            return result

        except Exception as e:
            result["status"] = "error"
            result["message"] = f"Wait failed: {e}"
            return result
        finally:
            if browser:
                await browser.close()


async def browser_extract_download(
    md5: str,
    mirror: str | None = None,
    timeout: int = 180,
    headless: bool = False,
) -> dict:
    """
    Navigate to the book page (or countdown page), wait for download
    link to appear, extract the token URL, and curl the file to /downloads.

    Handles the countdown → token URL flow that Anna's Archive uses
    after CAPTCHA is solved.

    Returns dict with:
      - status: "success" | "error" | "timeout"
      - file_path: absolute path to downloaded file (on success)
      - file_size: bytes
      - token_url: the final download URL
      - message: human-readable explanation
    """
    if not _check_playwright():
        return {"status": "error", "message": "Playwright not installed", "md5": md5}

    from playwright.async_api import async_playwright

    mirror_base = mirror or ANNAS_MIRRORS[0]
    page_url = f"{mirror_base}/md5/{md5}"

    result = {
        "md5": md5,
        "source_url": page_url,
        "mirror": mirror_base,
    }

    async with async_playwright() as p:
        browser = None
        try:
            browser, page = await _launch_browser(p, headless=headless)

            await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            # ── Step 1: Click the best download button ──
            # Look for any visible link/button that suggests "download"
            download_keywords = [
                "download", "slow partner", "partner server",
                "get file", "free download", "external download",
                "torrent", "pdf", "epub",
            ]
            all_links = await page.locator("a, button").all()
            scored = []
            for link in all_links:
                try:
                    if not await link.is_visible(timeout=1000):
                        continue
                except Exception:
                    continue
                try:
                    txt = (await link.text_content() or "").strip().lower()
                except Exception:
                    txt = ""
                href = ""
                try:
                    href = (await link.get_attribute("href")) or ""
                except Exception:
                    pass
                score = 0
                for i, kw in enumerate(download_keywords):
                    if kw in txt:
                        score += len(download_keywords) - i
                # Bonus: direct file links
                if href and any(
                    href.endswith(ext) for ext in (".epub", ".pdf", ".mobi", ".zip")
                ):
                    score += 10
                # Bonus: token URL patterns
                if "wbsg8v" in href or "/d3/y/" in href:
                    score += 100
                if score:
                    scored.append((score, link))
            scored.sort(reverse=True, key=lambda x: x[0])

            if scored:
                # Click the best candidate
                for _, link in scored:
                    try:
                        await link.click(timeout=5000)
                        await asyncio.sleep(3)
                        break
                    except Exception:
                        continue

            # ── Step 2: Poll for download token URL ──
            token_url = None
            start_time = asyncio.get_event_loop().time()

            while asyncio.get_event_loop().time() - start_time < timeout:
                await asyncio.sleep(3)

                # Check page URL
                current = page.url
                if "wbsg8v" in current or "/d3/y/" in current:
                    token_url = current
                    break

                # Check for token URL in DOM
                try:
                    found_raw = await page.evaluate("""
                        JSON.stringify(
                            Array.from(document.querySelectorAll(
                                'a[href], button[data-clipboard-text], input[value], textarea'
                            ))
                            .map(el => el.href || el.dataset?.clipboardText
                                      || el.value || el.textContent || '')
                            .filter(s => s.includes('wbsg8v')
                                      || s.includes('/d3/y/')
                                      || s.endsWith('.epub')
                                      || s.endsWith('.pdf')
                                      || s.endsWith('.mobi'))
                        )
                    """)
                    found = json.loads(found_raw)
                    if found:
                        token_url = found[0]
                        break
                except Exception:
                    pass

                # Check if download links appeared
                state = await _detect_state(page)
                if state.get("download_links"):
                    dl_links = state["download_links"]
                    # Prefer token URLs
                    for dl in dl_links:
                        u = dl.get("url", "")
                        if "wbsg8v" in u or "/d3/y/" in u:
                            token_url = u
                            break
                    if not token_url:
                        token_url = dl_links[0].get("url", "")
                    if token_url:
                        break

            if not token_url:
                # Last-ditch: screenshot for diagnosis
                screenshot_path = DOWNLOAD_DIR / f"stuck_{md5}.png"
                await page.screenshot(path=str(screenshot_path))
                return {
                    **result,
                    "status": "timeout",
                    "message": f"No download token URL found after {timeout}s",
                    "screenshot_path": str(screenshot_path),
                    "display_url": DISPLAY_URL,
                }

            # ── Step 3: curl the file ──
            filename = unquote(token_url.split("/")[-1].split("?")[0])
            if not filename or len(filename) < 4:
                filename = f"anna_{md5[:8]}.epub"
            output_path = DOWNLOAD_DIR / filename

            cookies = await page.context.cookies()
            cookie_str = "; ".join(
                [f"{c['name']}={c['value']}" for c in cookies if c.get("name")]
            )

            proc = await asyncio.create_subprocess_exec(
                "curl", "-L", "-s", "-o", str(output_path),
                "-H", f"Cookie: {cookie_str}",
                "-H",
                "User-Agent: Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
                "-H", f"Referer: {page.url}",
                "--max-time", "300",
                token_url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr_bytes = await proc.communicate()

            if proc.returncode == 0 and output_path.exists():
                file_size = output_path.stat().st_size
                if file_size > 1024:  # More than 1KB — real file
                    return {
                        **result,
                        "status": "success",
                        "message": f"Downloaded {filename} ({file_size} bytes)",
                        "file_path": str(output_path),
                        "file_size": file_size,
                        "token_url": token_url,
                    }

            return {
                **result,
                "status": "error",
                "message": f"curl failed (exit {proc.returncode}): "
                f"{stderr_bytes.decode()[:200] if stderr_bytes else 'no stderr'}",
                "token_url": token_url,
                "output_path": str(output_path),
            }

        except Exception as e:
            result["status"] = "error"
            result["message"] = f"Extraction failed: {e}"
            return result
        finally:
            if browser:
                await browser.close()


# ── Status check ─────────────────────────────────────────────────

async def browser_status() -> dict:
    """Check if Playwright is available."""
    result = {
        "display_url": DISPLAY_URL,
        "cdp_port": CDP_PORT,
    }
    if not _check_playwright():
        return {**result, "available": False, "reason": "playwright not installed"}
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox"],
            )
            await browser.close()
            return {
                **result,
                "available": True,
                "type": "local_chromium",
                "via": "container",
            }
    except Exception as e:
        return {**result, "available": False, "reason": str(e)}
