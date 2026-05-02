"""
Browser fallback for pirate-dock — CDP-driven, implements the full flow.

Architecture:
  run.sh launches persistent Chromium headed on :1 with --remote-debugging-port=9223.
  This module connects via Playwright's connect_over_cdp().
  Three public functions implement the agreed workflow:
    1. browser_navigate(md5)    → opens book page, expands External downloads, detects state
    2. browser_wait_for_change(md5, timeout) → polls for CAPTCHA solve/page change
    3. browser_extract_download(md5, timeout) → click slow server, handle DDoS→hCaptcha→countdown→curl

CDP endpoint: http://127.0.0.1:9223
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

ANNAS_MIRRORS = [
    "https://annas-archive.gl",
    "https://annas-archive.pk",
    "https://annas-archive.gd",
]


def _check_playwright() -> bool:
    try:
        from playwright.async_api import async_playwright  # noqa: F401
        return True
    except ImportError:
        return False


async def _connect_cdp(p):
    cdp_url = f"http://127.0.0.1:{CDP_PORT}"
    browser = await p.chromium.connect_over_cdp(cdp_url)

    contexts = browser.contexts
    if contexts:
        context = contexts[0]
    else:
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

    existing_pages = context.pages
    for ep in existing_pages:
        if ep != page:
            try:
                if ep.url in ("about:blank", ""):
                    await ep.close()
            except Exception:
                pass

    return browser, page


async def _detect_state(page) -> dict:
    """Detect what's on the current page. Returns state dict.
    
    CRITICAL: does NOT flag filepath metadata links (.epub, .pdf in hrefs)
    as download_ready. Only actual token URLs or action buttons count.
    """
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
    title_lower = title.lower()

    # DDoS-Guard
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

    # Check if we're on a /slow_download/ page
    if "/slow_download/" in url:
        # Look for "I am human" checkbox, countdown, or download token
        if any(x in body_lower for x in ["countdown", "please wait", "seconds until"]):
            return {"state": "countdown", "title": title, "url": url}
        if any(x in body_lower for x in ["i am human", "i'm human", "im not a robot"]):
            return {"state": "ddos_guard_checkbox", "title": title, "url": url}
        return {"state": "slow_download_page", "title": title, "url": url}

    # Cloudflare
    if any(x in body_lower for x in ["just a moment", "verifying you are human"]):
        return {"state": "cloudflare", "title": title, "url": url}

    # Countdown / timer
    if any(x in body_lower for x in ["countdown", "please wait", "seconds until"]):
        return {"state": "countdown", "title": title, "url": url}

    # Real download links — ONLY token URLs (wbsg8v, /d3/y/) or buttons
    # that are actual action buttons, NOT filepath metadata links
    try:
        links_raw = await page.evaluate("""
            JSON.stringify(
                Array.from(document.querySelectorAll('a[href]'))
                    .filter(a => {
                        const t = (a.textContent || '').toLowerCase();
                        const h = a.href || '';
                        // Only token URLs or explicit download action buttons
                        return h.includes('wbsg8v') 
                            || h.includes('/d3/y/')
                            || t.includes('slow download')
                            || t.includes('partner server')
                            || (t.includes('download') && !t.includes('search') && !t.includes('view in'));
                    })
                    .map(a => ({text: (a.textContent||'').trim().substring(0,100), url: a.href}))
            )
        """)
        links = json.loads(links_raw)
        if links:
            return {"state": "download_ready", "download_links": links, "title": title, "url": url}
    except Exception:
        pass

    # Check if "External downloads" section exists (book page — needs expanding)
    try:
        ext_dl = await page.evaluate("""
            (() => {
                const els = document.querySelectorAll('a, button, div, span, h2, h3');
                for (const el of els) {
                    const t = (el.textContent || '').toLowerCase();
                    if (t.includes('external download') || t.includes('download options')) {
                        return true;
                    }
                }
                return false;
            })()
        """)
        if ext_dl:
            return {"state": "book_page", "title": title, "url": url, "has_external_downloads": True}
    except Exception:
        pass

    return {"state": "book_page", "title": title, "url": url}


def _screenshot_b64(path_str: str) -> str | None:
    try:
        with open(path_str, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except Exception:
        return None


async def _click_external_downloads(page) -> bool:
    """Find and click the External downloads expander on the book page."""
    try:
        # Try clicking the "show external downloads" link
        clicked = await page.evaluate("""
            (() => {
                const links = document.querySelectorAll('a');
                for (const a of links) {
                    const t = (a.textContent || '').toLowerCase();
                    const h = (a.href || '').toLowerCase();
                    if (t.includes('external download') || t.includes('show external')
                        || h.includes('#external') || h.includes('downloads')) {
                        a.click();
                        return 'clicked_link';
                    }
                }
                const buttons = document.querySelectorAll('button, [role="button"], div[onclick]');
                for (const b of buttons) {
                    const t = (b.textContent || '').toLowerCase();
                    if (t.includes('external download') || t.includes('show external')) {
                        b.click();
                        return 'clicked_button';
                    }
                }
                return 'not_found';
            })()
        """)
        if clicked != 'not_found':
            await asyncio.sleep(3)
            return True
    except Exception:
        pass

    # Fallback: scroll to bottom and look for download links directly
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(2)
    except Exception:
        pass

    return False


async def _click_ddos_checkbox(page) -> bool:
    """On a DDoS-Guard page, try clicking 'I am human' checkbox."""
    try:
        result = await page.evaluate("""
            (() => {
                // Try multiple selectors for DDoS-Guard checkbox
                const selectors = [
                    'input[type="checkbox"]',
                    '.js-captcha-refresh',
                    'button',
                    'a',
                ];
                for (const sel of selectors) {
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {
                        const t = (el.textContent || '').toLowerCase();
                        const v = (el.value || '').toLowerCase();
                        const p = (el.placeholder || '').toLowerCase();
                        if (t.includes('human') || t.includes('not robot') 
                            || v.includes('human') || p.includes('human')
                            || t.includes("i'm") || t.includes('i am')) {
                            el.click();
                            return 'clicked_ddos';
                        }
                    }
                }
                return 'not_found';
            })()
        """)
        if result == 'clicked_ddos':
            await asyncio.sleep(3)
            return True
    except Exception:
        pass
    return False


# ── Public API ──────────────────────────────────────────────────

async def browser_navigate(
    md5: str,
    mirror: str | None = None,
) -> dict:
    """
    Navigate to an Anna's Archive book page, expand External downloads,
    and detect the real state.

    Returns dict with status, state, screenshot, and context for next step.
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
        "method": "cdp_connect",
    }

    async with async_playwright() as p:
        browser = None
        try:
            browser, page = await _connect_cdp(p)

            await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(4)

            # Detect initial state
            state = await _detect_state(page)

            # If on a book page, try to expand External downloads
            if state["state"] == "book_page":
                expanded = await _click_external_downloads(page)
                if expanded:
                    await asyncio.sleep(2)
                    state = await _detect_state(page)

            # Screenshot
            screenshot_path = DOWNLOAD_DIR / f"navigate_{md5}.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            result["screenshot_path"] = str(screenshot_path)
            screenshot_b64 = _screenshot_b64(str(screenshot_path))
            if screenshot_b64:
                result["screenshot_b64"] = screenshot_b64

            result["state"] = state["state"]
            result["page_title"] = state.get("title", "")
            if state.get("download_links"):
                result["download_links"] = state["download_links"]
            if state.get("has_external_downloads"):
                result["has_external_downloads"] = True

            result["status"] = "ok"

            # Build human-readable message
            state_map = {
                "download_ready": "Download links found — ready to extract",
                "captcha_visual": f"hCaptcha detected — David solve at {DISPLAY_URL}",
                "ddos_guard_js": "DDoS-Guard JS challenge — waiting for auto-redirect",
                "ddos_guard_manual": f"DDoS-Guard manual check — David interact at {DISPLAY_URL}",
                "ddos_guard_checkbox": "'I am human' checkbox visible — auto-click possible",
                "countdown": "Countdown timer visible — waiting for download",
                "cloudflare": "Cloudflare challenge detected",
                "book_page": "Book page loaded — External downloads section may need expanding",
                "slow_download_page": "On slow download page",
            }
            result["message"] = state_map.get(state["state"], f"State: {state['state']}")

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
) -> dict:
    """
    Wait for page state to change after human solves CAPTCHA via VNC.
    Polls the page, returns new state when it changes.
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
        "method": "cdp_connect",
    }

    async with async_playwright() as p:
        browser = None
        try:
            browser, page = await _connect_cdp(p)

            await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            initial_url = page.url
            start_time = asyncio.get_event_loop().time()

            for _ in range(timeout // 2):
                await asyncio.sleep(2)
                current_url = page.url
                state = await _detect_state(page)
                
                # Changed if URL different or state is actionable
                if current_url != initial_url and current_url != "about:blank":
                    break
                if state["state"] in ("countdown", "download_ready", "slow_download_page"):
                    break

            elapsed = asyncio.get_event_loop().time() - start_time
            result["waited_seconds"] = round(elapsed, 1)
            result["initial_url"] = initial_url
            result["final_url"] = page.url

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

            if result["final_url"] == initial_url and state["state"] in ("book_page", "unknown"):
                result["status"] = "timeout"
                result["message"] = f"Page unchanged after {elapsed}s. CAPTCHA may not be solved."
            elif state["state"] in ("download_ready", "countdown"):
                result["status"] = "ok"
                result["message"] = f"Page advanced to: {state['state']}"
            else:
                result["status"] = "ok"
                result["message"] = f"State: {state['state']}"

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
    timeout: int = 300,
) -> dict:
    """
    Full extraction flow:
    1. Navigate to book page, expand external downloads
    2. If download links exist → click slow partner server → DDoS-Guard
    3. Handle DDoS checkbox → hCaptcha (send to David if visible)
    4. Wait for countdown → token URL → curl to /downloads
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
        "method": "cdp_connect",
    }

    async with async_playwright() as p:
        browser = None
        try:
            browser, page = await _connect_cdp(p)

            # ── Phase 1: Get to the book page and expand downloads ──
            await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(4)

            state = await _detect_state(page)
            
            # If book page, expand external downloads
            if state["state"] == "book_page":
                await _click_external_downloads(page)
                await asyncio.sleep(3)
                state = await _detect_state(page)

            # ── Phase 2: If download links visible, click slow partner server ──
            if state["state"] == "download_ready":
                dl_links = state.get("download_links", [])
                slow_found = False
                for dl in dl_links:
                    t = dl.get("text", "").lower()
                    if "slow download" in t or "partner server" in t:
                        # Find and click this specific link
                        try:
                            await page.evaluate(f"""
                                (() => {{
                                    const links = document.querySelectorAll('a');
                                    for (const a of links) {{
                                        const txt = (a.textContent || '').toLowerCase();
                                        const hr = (a.href || '');
                                        if ((txt.includes('slow download') || txt.includes('partner server'))
                                            && hr === '{dl["url"]}') {{
                                            a.click();
                                            return;
                                        }}
                                    }}
                                }})()
                            """)
                            slow_found = True
                            await asyncio.sleep(5)
                            break
                        except Exception:
                            continue

                if slow_found:
                    # Now we should be on DDoS-Guard or countdown page
                    state = await _detect_state(page)

            # ── Phase 3: Handle DDoS-Guard checkbox ──
            if state["state"] in ("ddos_guard_checkbox", "ddos_guard_manual", "ddos_guard_js"):
                if state["state"] == "ddos_guard_checkbox":
                    # Try auto-clicking "I am human"
                    clicked = await _click_ddos_checkbox(page)
                    if clicked:
                        await asyncio.sleep(5)
                        state = await _detect_state(page)

            # ── Phase 4: hCaptcha — can't auto-solve, return to caller ──
            if state["state"] == "captcha_visual":
                screenshot_path = DOWNLOAD_DIR / f"captcha_{md5}.png"
                await page.screenshot(path=str(screenshot_path))
                return {
                    **result,
                    "status": "captcha_required",
                    "state": "captcha_visual",
                    "message": f"hCaptcha needs solving: {DISPLAY_URL}",
                    "screenshot_path": str(screenshot_path),
                    "display_url": DISPLAY_URL,
                }

            # ── Phase 5: Poll for token URL (countdown → download) ──
            token_url = None
            start_time = asyncio.get_event_loop().time()

            while asyncio.get_event_loop().time() - start_time < timeout:
                await asyncio.sleep(3)

                current_url = page.url
                body_text = ""
                try:
                    body_text = await page.evaluate("() => document.body?.innerText || ''")
                except Exception:
                    pass

                # Direct URL match
                if "wbsg8v" in current_url or "/d3/y/" in current_url:
                    token_url = current_url
                    break

                # Search DOM for token URLs
                try:
                    found = await page.evaluate("""
                        JSON.stringify(
                            Array.from(document.querySelectorAll(
                                'a[href], button[data-clipboard-text], input[value], textarea'
                            ))
                            .map(el => el.href || el.dataset?.clipboardText
                                      || el.value || el.textContent || '')
                            .filter(s => s.includes('wbsg8v') || s.includes('/d3/y/')
                                      || (s.endsWith('.epub') && s.startsWith('http'))
                                      || (s.endsWith('.pdf') && s.startsWith('http')))
                        )
                    """)
                    found_urls = json.loads(found)
                    if found_urls:
                        token_url = found_urls[0]
                        break
                except Exception:
                    pass

                # Check if page has countdown text
                if any(x in body_text.lower() for x in ["seconds until", "minutes until"]):
                    result["message"] = "Countdown timer active — waiting..."
                    continue

            if not token_url:
                screenshot_path = DOWNLOAD_DIR / f"stuck_{md5}.png"
                await page.screenshot(path=str(screenshot_path))
                return {
                    **result,
                    "status": "timeout",
                    "state": "no_token_url",
                    "message": f"No download URL found after {timeout}s. Last URL: {page.url[:120]}",
                    "screenshot_path": str(screenshot_path),
                    "display_url": DISPLAY_URL,
                }

            # ── Phase 6: curl the file ──
            filename = unquote(token_url.split("/")[-1].split("?")[0])
            if not filename or len(filename) < 4:
                filename = f"anna_{md5[:8]}.epub"
            
            # Sanitise filename — remove colons and excessive length
            filename = filename.replace(":", "-").replace(" ", "_")
            if len(filename) > 120:
                name, ext = os.path.splitext(filename)
                filename = f"{name[:80]}{ext}"

            output_path = DOWNLOAD_DIR / filename

            cookies = await page.context.cookies()
            cookie_str = "; ".join(
                [f"{c['name']}={c['value']}" for c in cookies if c.get("name")]
            )

            proc = await asyncio.create_subprocess_exec(
                "curl", "-L", "-s", "-o", str(output_path),
                "-H", f"Cookie: {cookie_str}",
                "-H", "User-Agent: Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
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
                if file_size > 1024:
                    return {
                        **result,
                        "status": "success",
                        "state": "downloaded",
                        "message": f"Downloaded {filename} ({file_size:,} bytes)",
                        "file_path": str(output_path),
                        "file_size": file_size,
                        "token_url": token_url,
                    }

            return {
                **result,
                "status": "error",
                "state": "curl_failed",
                "message": f"curl failed (exit {proc.returncode}): "
                f"{stderr_bytes.decode()[:200] if stderr_bytes else 'no stderr'}",
                "token_url": token_url,
                "output_path": str(output_path),
            }

        except Exception as e:
            result["status"] = "error"
            result["state"] = "exception"
            result["message"] = f"Extraction failed: {e}"
            return result
        finally:
            if browser:
                await browser.close()


# ── Status check ─────────────────────────────────────────────────

async def browser_status() -> dict:
    """Check if persistent Chromium CDP is reachable."""
    result = {"display_url": DISPLAY_URL, "cdp_port": CDP_PORT}
    import httpx
    try:
        r = httpx.get(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=3)
        if r.status_code == 200:
            return {
                **result,
                "available": True,
                "type": "persistent_chromium",
                "via": "cdp",
                "browser": r.json().get("Browser", "unknown"),
            }
    except Exception:
        pass
    return {**result, "available": False, "reason": "CDP not reachable"}
