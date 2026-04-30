# Pirate Dock v3.3 — Automation Rebuild

**Started:** 2026-04-30 ~22:00
**Model:** deepseek-v4-pro:cloud
**Goal:** Replace broken monolithic browser_fallback.py with CDP-driven control. Actually download a book.

---

## What We Learned (v3.2, 2026-04-30)

### What WORKS
- **Display stack:** Xvfb :1 → x11vnc :5900 → websockify :6081 → noVNC. Rock solid.
- **VPN:** NordVPN South Africa, NordLynx P2P, killswitch on. Bridge networking. Docker subnet whitelisted.
- **API/Jackett:** FastAPI on :9876, Jackett on :9118. Both operational.
- **Chromium inside container:** Playwright Chromium 1217 launches headed on :1. `--no-sandbox` required.
- **VNC visibility:** `https://araminta.taild3f7b9.ts.net/pirate/vnc_lite.html?path=pirate%2F` — David can see the browser.

### What DOES NOT WORK

1. **Camoufox headless + DDoS-Guard JS challenges.** The JS redirect never fires in headless mode. The DDoS-Guard "checking your browser" page just sits there indefinitely. Tier 1 of our fallback is dead on arrival.

2. **hCaptcha checkbox auto-click via Playwright/CDP.** The checkbox lives in a cross-origin `<iframe>` from `hcaptcha.com`. Playwright's `frame.wait_for_selector` can find the iframe but cannot reach *into* it to click the checkbox — same-origin policy blocks it. CDP's `Runtime.evaluate` hits the same wall. There is no programmatic way to click hCaptcha's "I am human" checkbox from outside the iframe.

3. **Anna's Archive DOM has changed (2026-04).** The "Slow Partner Server" button that `browser_fallback.py` hunts for no longer exists. Z-Library mirrors return 503. The download path has shifted to different buttons/links.

4. **Camoufox adds complexity without benefit.** The only advantage over plain Chromium is anti-fingerprinting, but Anna's Archive's DDoS-Guard doesn't fingerprint aggressively enough for it to matter — it uses JS challenges + hCaptcha, both of which block Camoufox and Chromium equally.

### Architecture Decision

**Abandon the monolithic "launch browser → find buttons → auto-click → hope" model.** The script has too many assumptions about AA's DOM baked in, and every AA change breaks it.

Instead: **CDP-driven control.** Chromium launches with `--remote-debugging-port=9223`. Minty controls it step-by-step via CDP — navigate, evaluate JS to find elements, click, screenshot, detect page changes. David only touches visual hCaptcha puzzles via the VNC link.

This mirrors exactly how the host browser stack works (browser-setup skill, port 9222 pattern) — proven and reliable.

---

## v3.3 Rebuild Plan

### Phase 1: Rewrite browser_fallback.py
Replace 400-line monolith with three clean functions:
- `navigate(md5)` — opens book page, returns screenshot + page state
- `wait_for_page_change(timeout)` — polls page URL, returns when navigation happens
- `extract_download()` — finds download links, curls file to /downloads

No Camoufox. No hCaptcha auto-click. No button-hunting heuristics.

### Phase 2: CDP control endpoint
Add `GET /browser/cdp` that returns the CDP WebSocket URL so Minty can connect directly.
Chromium launches from run.sh with `--remote-debugging-port=9223`.

### Phase 3: Rebuild & test
- `docker compose down`
- Update Dockerfile (remove Camoufox dep if clean, or keep it isolated)
- `bash scripts/build.sh --no-cache`
- Test: launch browser, navigate to AA, actually try to download Japaneasy

### Phase 4: Leave status for David
By morning David should see:
- Container running, VPN connected
- Browser launched on AA book page
- Either: download complete (file in /downloads) OR VNC link with hCaptcha waiting
- Status messages documenting every step

---

## Build & Test Log

| Time | Action | Result |
|------|--------|--------|
| 22:00 | Learnings documented, rebuild begins | — |
