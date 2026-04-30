# Pirate Dock v3.3 — Automation Rebuild

**Started:** 2026-04-30 ~22:00
**Model:** deepseek-v4-pro:cloud
**Goal:** Replace broken monolithic browser_fallback.py with CDP-driven control. Actually download a book.

---

## Session Outcome: PARTIAL SUCCESS

**Container:** Fully operational. Rebuilds clean, VPN connects, CDP reachable, display stack visible via VNC.
**Book page:** Loads without CAPTCHA on first visit.
**Download:** BLOCKED by hCaptcha trigger on download button click.

---

## Critical Learnings (2026-04-30, final)

### What WORKS
- **Display stack:** Xvfb :1 → x11vnc :5900 → websockify :6081 → noVNC. Rock solid.
- **VPN:** NordVPN South Africa, NordLynx P2P, killswitch on. Bridge networking. Docker subnet whitelisted.
- **API/Jackett:** FastAPI on :9876, Jackett on :9118. Both operational.
- **Persistent Chromium CDP:** Launched once by run.sh, connect_over_cdp() works. Port 9223.
- **VNC visibility:** David can see and interact with the browser.
- **Human CAPTCHA solve:** David solved hCaptcha via VNC — it works. The persistent browser tab advanced past the challenge.
- **Three-step API design:** navigate/wait/extract is the right abstraction.

### What DOES NOT WORK

1. **hCaptcha sessions are PER-TAB, not per-browser.** The fundamental mistake: browser_extract_download() calls connect_over_cdp() which creates a NEW tab, navigates fresh, and gets its OWN hCaptcha challenge. David solving via VNC in a different tab (the persistent about:blank that run.sh launched) does nothing for the extract tab. The extract tab polls for token URLs that will never appear because it's stuck on its own hCaptcha wall.

2. **Extract timeouts mask the VNC gap.** The 180s timeout in browser_extract_download expires long after David might have solved the CAPTCHA in the wrong tab. The function returns "timeout" but the real issue is that it never connected to the right tab.

3. **wait_for_change was never wired into the flow.** This function exists and is correct — it navigates to the book page and polls URL changes. But the production flow went navigate → extract, skipping wait entirely. The extract function has its own internal polling that's designed for the old monolithic approach.

4. **No tab-sharing between functions.** Each function (navigate, wait, extract) calls connect_over_cdp independently, creating separate Playwright connections. There's no mechanism for "use the same tab David just solved in."

### The Fix (for tomorrow)

The three-step flow MUST use the same browser tab throughout:

**Option A: Single-session approach (recommended for now)**
- browser_navigate() creates a page, navigates to book page
- If captcha_visual: return VNC link, but KEEP THE PAGE OPEN (don't close browser)
- David solves via VNC on that same page
- browser_wait_for_change() should connect and find the EXISTING page that already passed hCaptcha, or create a new one that re-navigates (cookies/session may persist)
- browser_extract_download() should use the same approach

**Option B: Cookie/session persistence**
- The persistent Chromium shares cookies across tabs
- After David solves hCaptcha, the session cookie is set
- A fresh navigation in a new tab should bypass the challenge
- This MAY work but isn't guaranteed — DDoS-Guard may still challenge new tabs

**Option C: Direct CDP control (most reliable)**
- Don't use Playwright's connect_over_cdp at all
- Use raw CDP commands to find the existing tab, evaluate JS, click elements, detect URL changes
- This avoids the "new connection = new tab" problem entirely

### For tomorrow's iPhone production test

1. Navigate: trigger browser_navigate, if captcha → send VNC link
2. David solves via VNC on phone
3. Immediately call browser_extract_download — the persistent Chromium may have cached the session and a fresh navigation might sail through. If not, we iterate.

---

## Build & Test Log

| Time | Action | Result |
|------|--------|--------|
| 22:00 | Learnings documented, rebuild begins | — |
| 22:05 | browser_fallback.py rewritten (CDP-first, 3 functions) | Committed 521fc51 |
| 22:10 | run.sh updated — persistent Chromium + CDP | Committed c9cc1a0 |
| 22:12 | Dockerfile cleaned — Camoufox removed | Committed c9cc1a0 |
| 22:15 | server.py updated — 3-step API (navigate/wait/extract) | Committed ff9a8fd |
| 22:18 | Chromium binary auto-discovery fix | Committed 2c4b3dc |
| 22:20 | `docker compose down` + `bash scripts/build.sh --no-cache` | BUILDING in background |
| 22:40 | Build complete, container up, VPN connected | ✓ API OK, CDP OK, Jackett OK |
| 22:42 | Navigate to Japaneasy page (browser_navigate) | ✓ download_ready — page loaded clean, no challenge |
| 22:45 | Extract download (browser_extract_download run 1) | hCaptcha triggered after clicking download button |
| 22:50 | Status left for David — VNC link waiting | Awaiting human puzzle solve |
| 23:03 | Production run: navigate | ✓ download_ready again |
| 23:04 | Production run: extract (run 2) | hCaptcha — stuck screenshot captured |
| 23:08 | David solved hCaptcha via VNC | ✓ Human solve works, page advanced |
| 23:09 | Production run: extract (run 3) | curl exit 28 — connection lost |
| 23:15 | Session halted by David | Learnings captured below |
