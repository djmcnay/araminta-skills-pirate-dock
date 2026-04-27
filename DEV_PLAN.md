# Pirate Dock — Dev Plan

**Last updated:** 2026-04-27 (post-unfuck)
**Status:** Image needs rebuild after today's fixes.

---

## Architecture

```
Container (NordVPN tunnel)
  ├── Xvfb :1          — virtual framebuffer for headed browser
  ├── x11vnc           — attaches to Xvfb, speaks VNC on localhost:5901
  ├── websockify        — bridges VNC → WebSocket on port 5998 (noVNC)
  ├── openbox           — window manager on :1
  ├── Playwright/Chromium — headed or headless, DISPLAY=:1
  ├── Camoufox/Firefox  — stealth headless browser
  ├── Jackett           — torrent indexer on port 9118
  └── FastAPI           — REST API on port 9876

Host (Pi)
  ├── 127.0.0.1:9876 → container:9876  (API — localhost only)
  ├── 127.0.0.1:9118 → container:9118  (Jackett — localhost only)
  └── 0.0.0.0:5998   → container:5998  (noVNC — Tailscale-reachable)

noVNC URL: http://100.65.212.67:5998/vnc.html
```

## CAPTCHA / Human-in-the-loop flow

1. Browser navigates Anna's Archive book page inside the VPN tunnel.
2. On visual challenge (CAPTCHA / DDoS-Guard): screenshot saved to `/downloads/captcha_{md5}.png`.
3. API returns `status: captcha_required` with:
   - `screenshot_b64` — PNG as base64 (Minty can pass to her vision model)
   - `screenshot_path` — path inside container (bind-mounted to host)
   - `novnc_url` — `http://100.65.212.67:5998/vnc.html`
   - `message` — human-readable prompt
4. Minty attempts vision solve if her model supports it. If not, she sends the noVNC URL to David via WhatsApp.
5. David opens the URL, solves the CAPTCHA in his browser.
6. The browser inside the container detects the page change and resumes.

**The container does NOT call any external LLM/vision API.** Vision is Minty's responsibility.

## Principles (never violate)

1. All VPN/sneaky traffic originates inside the container. Host Pi never runs NordVPN.
2. Container is disposable: `docker compose down && up` restores everything.
3. API and Jackett ports are localhost-only. noVNC port is Tailscale-accessible (not public internet).
4. NordVPN whitelist is auto-configured in run.sh on every start.

## Rebuild command

```bash
cd ~/Documents/GitHub/pirate-dock
docker compose down
docker compose build --no-cache
docker compose up -d
```

## Verify after rebuild

```bash
# VPN connected
docker exec pirate-dock nordvpn status

# API up
curl -s http://127.0.0.1:9876/health

# Browser stack
curl -s http://127.0.0.1:9876/browser/status

# noVNC reachable (should return HTML)
curl -s http://100.65.212.67:5998/vnc.html | head -3
```

## File inventory

```
~/Documents/GitHub/pirate-dock/
├── Dockerfile              — Xvfb + x11vnc + noVNC + Playwright + Camoufox + Jackett
├── docker-compose.yml      — ports, env vars (incl. NOVNC_URL), volumes
├── .env                    — NORDVPN_TOKEN (not in git)
├── DEV_PLAN.md             — this file
├── SKILL.md                — what Minty reads
└── scripts/
    ├── run.sh              — entrypoint: display stack → VPN → Jackett → uvicorn
    ├── server.py           — FastAPI routes
    ├── browser_fallback.py — Playwright browser automation + CAPTCHA handling
    └── requirements.txt    — Python deps
```

## Known issues / next steps

- **Camoufox integration**: camoufox is installed in the image but `browser_fallback.py`
  currently uses Playwright/Chromium for all paths. A future session should add a
  camoufox-first headless path with Playwright/Chromium as the headed fallback.
- **Jackett API key**: stored in `/data/jackett/ServerConfig.json` (persisted volume).
  First run generates it; subsequent runs read it from volume.
