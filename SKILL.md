---
name: pirate-dock
description: Control Pirate Dock — a bespoke Docker container for VPN-protected ebook and media downloads. Searches Anna's Archive and torrent indexers behind NordVPN. Auto-downloads via torrent when possible, sends manual download links when automation can't bypass CAPTCHAs.
category: media
---

# Pirate Dock Skill

## Overview
A custom-built Docker container running NordVPN (South Africa, P2P), aria2 for downloads, and Jackett for multi-site torrent search. Two parallel pipelines: Anna's Archive for ebooks, Jackett for torrents/video.

**Architecture:** Headless first (HTTP scraping), browser fallback via Playwright + Chromium running **inside** the container when CAPTCHAs, DDoS-Guard, login, or visual confirmation block the normal path. All traffic stays behind NordVPN inside the container. Bridge networking isolates the container's VPN from the host Pi. The human-in-the-loop display is not optional: Minty must be able to send David a URL that shows the container browser.
**Project home:** `~/Documents/GitHub/pirate-dock/`
**Container name:** `pirate-dock`
**API:** `http://localhost:9876` (published port — no `docker exec` needed)
**Jackett UI:** `http://localhost:9118` (published port)
**Human browser display:** `https://araminta.taild3f7b9.ts.net/pirate/vnc_lite.html?path=pirate%2F` (public HTTPS via Tailscale Funnel)
**Browser fallback:** Container-local Playwright/Chromium — launched inside `pirate-dock`; zero host CDP/browser dependency. Headed Chromium uses container display `:1`, x11vnc exports it on `localhost:5900`, websockify bridges VNC→WebSocket on `0.0.0.0:6081` and serves the noVNC HTML5 client from `/usr/share/novnc`. The `path` URL parameter ensures WebSocket traffic routes through Tailscale Funnel's `/pirate/` prefix.

**Tailscale Funnel invariant:** `https://araminta.taild3f7b9.ts.net/pirate/` must proxy to `http://127.0.0.1:6081`. Check with `sudo tailscale funnel status`. Repair with `sudo tailscale funnel --bg --https=443 --set-path=/pirate 6081`. Use Funnel for browser access, and do not overwrite unrelated root routes on `https://araminta.taild3f7b9.ts.net/`.

---

## Container Management

### Build & start
```bash
cd ~/Documents/GitHub/pirate-dock
# ALWAYS use the safe build script (checks disk space)
bash scripts/build.sh
```

**After container start:** `run.sh` auto-whitelists the Docker bridge subnet (`172.16.0.0/12`) and published ports (9876, 9118, **6081**) inside NordVPN's killswitch. This is required for the host Pi and Tailscale Funnel to reach the FastAPI/Jackett/websockify endpoints while NordVPN is active. If you manually change ports or networking, the whitelist must match.

### Browser display URL
This is the URL Minty should send by WhatsApp when human intervention is needed:

```
https://araminta.taild3f7b9.ts.net/pirate/vnc_lite.html?path=pirate%2F
```

The `path=pirate%2F` parameter is critical — it tells noVNC to route its WebSocket through `/pirate/` so Tailscale Funnel can proxy it correctly. Without it, noVNC connects to the root WebSocket path and Funnel drops it.

### noVNC display stack (inside container)
When automation hits a visual challenge, `browser_fallback.py` launches Chromium in headed mode on container display `:1`. `run.sh` starts the full display stack:

```
Xvfb :1              → virtual framebuffer (1280x800x24)
x11vnc -display :1   → exports display as VNC on localhost:5900
websockify :6081 :5900 --web=/usr/share/novnc  → bridges VNC→WebSocket, serves noVNC HTML
```

The user connects through `https://araminta.taild3f7b9.ts.net/pirate/vnc_lite.html?path=pirate%2F` → Tailscale Funnel strips `/pirate/` prefix → reaches websockify on `:6081` → bridges to x11vnc on `:5900` → displays Xvfb `:1` with Chromium visible.

### Check status
```bash
curl -sf http://localhost:9876/status | python3 -m json.tool
```

### View logs
```bash
docker logs pirate-dock --tail 50
```

### Stop
```bash
cd ~/Documents/GitHub/pirate-dock
docker compose down
```

### Cleanup (if disk space low)
```bash
bash scripts/prune-docker.sh            # Standard cleanup
bash scripts/prune-docker.sh --aggressive  # Nuclear option
```

### Skill Tests
```bash
# Functional tests (run from host — container must be running)
python3 scripts/test.py

# Host isolation safety tests (MUST pass before any docker-compose.yml changes)
python3 scripts/test_isolation.py
```

Functional tests (test.py) cover:
1. Anna's Archive search — finds a book and generates download links
2. Jackett torrent search — lists top 10 UFC video results
3. Download lifecycle — starts a torrent download, cancels it, deletes partial files

Isolation tests (test_isolation.py) cover:
- Host iptables are never modified by the container (baseline, while running, after stop)
- Host Pi can always reach Discord, GitHub, Google while container runs
- Container is confirmed on bridge networking (not host)
- API and Jackett are accessible via published ports

---

## API Reference (`http://localhost:9876`)

### VPN

| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| GET | `/status` | — | VPN + Jackett status |
| POST | `/vpn/connect` | `{"country": "South_Africa"}` | Connect VPN |
| POST | `/vpn/disconnect` | — | Disconnect VPN |

### Anna's Archive (eBooks)

| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| GET | `/search/annas-archive?q=...` | — | Search books by title/author/ISBN |
| POST | `/search/annas-archive` | `{\"query\": \"...\"}` | Search (POST version) |
| GET | `/download/annas-archive/{md5}` | — | Get download info by MD5 |
| POST | `/download/annas-archive` | `{\"md5\": \"...\"}` | Download by MD5 hash |
| POST | `/download/annas-archive/browser` | `{\"md5\": \"...\"}` | **Browser fallback** — navigate with container Playwright, wait for human CAPTCHA solve if needed |
| GET | `/download/annas-archive/{md5}/browser` | — | **Browser fallback** (GET convenience) |
| GET | `/browser/status` | — | Check if container browser stack is available and return the display URL |

### Torrent Search (via Jackett)

| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| GET | `/search/torrents?q=...` | — | Search all configured indexers |
| GET | `/search/piratebay?q=...` | — | Search PirateBay only |
| GET | `/search/1337x?q=...` | — | Search 1337x only |
| GET | `/search/ext?q=...` | — | Search ext.to only |
| GET | `/jackett/indexers` | — | List available indexers |

### Downloads

| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| POST | `/download/magnet` | `{"magnet": "..."}` | Download via aria2 |
| GET | `/downloads/active` | — | Running aria2 processes |
| GET | `/downloads/list` | — | Files in /downloads |

### UFC Watch (background poller)

| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| POST | `/watch/ufc` | `{"event": "UFC 327"}` | Start watching for event |
| GET | `/watch/ufc` | — | Status of all watches |
| DELETE | `/watch/ufc/{key}` | — | Stop watching |

---

## Core Workflow: Book Request

When a user asks for a book (by title, Amazon link, Goodreads link, ISBN, or MD5), follow this sequence:

### Step 1: Identify the book
- If given a URL (Amazon, Goodreads): resolve it to get the ISBN/title
- If given a title: use it directly

### Step 2: Search both pipelines in parallel (behind NordVPN)
```
Anna's Archive:  GET /search/annas-archive?q=<title+author+isbn>
Torrent:         GET /search/torrents?q=<title+author+isbn>
```

### Step 3: Evaluate results

**A) Torrent found with seeders ≥ 2 → AUTO-DOWNLOAD**
```
POST /download/magnet {"magnet": "<best_magnet_link>"}
```
Report: title, size, seeders. The file will land in `/downloads` inside the container.

**B) No torrent with seeders → SEND ANNA'S ARCHIVE LINK**
- Report what was found: title, MD5, file size, available formats (EPUB/PDF/MOBI)
- Provide the Anna's Archive page link: `https://annas-archive.gl/md5/{md5}`
- List the mirror links (`.gl`, `.pk`, `.gd`)
- User clicks the link and completes the download manually

**C) Nothing found on either pipeline → report that clearly**

### Step 4: Report results

Always provide:
1. **What the book is** (title, author, format, size)
2. **Auto-download status** (downloaded / link to click)
3. **Anna's Archive link** (always include, as fallback)
4. **Torrent details** (if applicable: seeders, source indexer)

---

## Download Reality

**What works automatically:**
- ✅ Anna's Archive **search** — reliable scraping of search results
- ✅ Jackett **torrent search** — TPB, 1337x, LimeTorrents, YTS, EZTV configured
- ✅ **aria2 downloads** via magnet — fast when seeders are healthy
- ✅ **Container infrastructure** — VPN, Playwright/Chromium, API, Jackett all operational

**What does NOT work automatically:**
- ❌ Anna's Archive **free book download automation** (2026-04-26). The container-local browser stack works, but AA's book page DOM has changed. The "Slow Partner Server" button no longer exists. Z-Library mirrors (`.gd`, `.se`, `.li`) return 503 or redirect to parking pages.

**The browser fallback flow (book page navigation) — currently STALLED:**
1. Headless scraping tries first (fast, no browser needed)
2. If blocked by CAPTCHA/DDoS-Guard or no direct links, fall back to browser mode automatically
3. Playwright launches Chromium **inside** `pirate-dock`; every request stays inside the container's NordVPN tunnel
4. Use the working mirror `https://annas-archive.gl` (the `.li` mirror was redirecting to parking/spam)
5. Match the browser fingerprint to South Africa: timezone `Africa/Johannesburg`, locale `en-GB`, user-agent `Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36`
6. **BLOCKED HERE**: AA's Downloads page now shows Z-Library mirrors, but Z-Library itself is down (503). The old "Slow Partner Server" path is gone.
7. **Human-in-the-loop fallback**. When automation fails, Chromium can launch in **headed mode** on display `:1`, visible through `https://araminta.taild3f7b9.ts.net/pirate/vnc_lite.html?path=pirate%2F`. A human can interact with the browser inside the VPN tunnel to solve CAPTCHAs or click download links manually. The script then captures the resulting download URL or file.
8. File downloads via `curl --insecure --location` to `/downloads` — only works if a valid download URL is found

**What does NOT work automatically (known gap):**
- ❌ AA free book downloads — need a new download path or redesign to torrent/tor mirrors
- ❌ Title parsing is still flaky in some AA layouts — MD5 extraction works reliably, but titles can still show as "Unknown" when the nested DOM shifts.

**Architecture note:** The container keeps the browser work inside the same network namespace as NordVPN; no host CDP browser stack is required for the AA flow. Bridge networking is still the right choice. Host networking remains forbidden — see safety note below.

**Seeder count caveat:** Torznab seeder counts (especially from TPB) may show 0 even when torrents are alive and downloadable. Always try downloading before reporting "no seeders" to the user.

---

## Workflow: Torrent Search (Jackett)

1. Jackett runs inside the container with 619 indexer definitions loaded
2. **Configured public indexers:** The Pirate Bay, 1337x, LimeTorrents, YTS, EZTV
3. Search: `GET /search/torrents?q=UFC+327`
4. Returns results with title, size, seeders, magnet link
5. Download: `POST /download/magnet {"magnet": "magnet:..."}`

**To add more indexers:** access Jackett web UI at `http://localhost:9118` from a machine that can reach the Pi.

## Workflow: UFC Event Watch

1. `POST /watch/ufc {"event": "UFC 327", "quality": "1080"}`
2. Background poller searches all indexers every 5 min
3. Filters: event name match + quality (1080p) + seeders >= 2
4. Check status: `GET /watch/ufc`
5. When found: `POST /download/magnet` with best match
6. Stop watching: `DELETE /watch/ufc/ufc_327`

---

## Credentials & Config

- **NORDVPN_TOKEN:** In `~/Documents/GitHub/pirate-dock/.env` + bind-mounted as `scripts/token.txt`
- **Jackett API key:** Auto-detected on startup from `/data/jackett/ServerConfig.json`
- **Jackett config:** Via web UI at `http://localhost:9118` (inside container)
- **NordVPN default region:** South Africa (NordLynx P2P)
- **Anna's Archive secret key:** In memory — free account, used for authenticated browsing (metadata only, downloads still CAPTCHA-gated)

---

## Known Issues — RESOLVED

- VPN login bug (2026-04-14). Token read from bind-mounted file (`/run/pirate-dock/token`) instead of env vars (s6-overlay strips env vars).
- Jackett indexers (2026-04-14). TPB, 1337x, LimeTorrents, YTS, EZTV all enabled and returning results.
- Anna's Archive search URL (2026-04-14). AA changed `/s?q=` to `/search?q=`. Fixed in `server.py`.
- Anna's Archive HTML parser (2026-04-14). New UI uses `.js-aarecord-list-outer` container with flex/border-b child divs. Updated `_parse_annas_search()`.
- Jackett startup deadlock (2026-04-14). `_start_jackett()` now checks for already-running Jackett before starting a new process; accepts HTTP 302 in addition to 200 (Jackett returns 302 for the indexers endpoint).
- NordVPN killswitch leaked to host Pi (2026-04-17). RESOLVED. Root cause: `network_mode: host` + `CAP_NET_ADMIN` caused NordVPN's iptables killswitch to apply to the Pi's own network namespace, blocking Discord, GitHub, and all non-local Pi connectivity for ~12 hours. Fix: switched to bridge networking — NordVPN's killswitch now operates inside the container's own namespace and physically cannot affect the host. The `test_isolation.py` suite is a regression guard.
- Docker bridge + killswitch blocked host-to-container API (2026-04-26). RESOLVED. When NordVPN connects inside the container with killswitch enabled, Docker bridge traffic (from host `172.19.0.1`) was dropped. Fix: `run.sh` now auto-whitelists the Docker bridge subnet and published ports (9876, 9118, **6081**) via `nordvpn whitelist add subnet 172.16.0.0/12`, `nordvpn whitelist add port 9876`, `nordvpn whitelist add port 9118`, `nordvpn whitelist add port 6081`. Container must restart to apply. The host can now reach the FastAPI, Jackett, and noVNC endpoints while NordVPN is active.
- Playwright runtime installation failure (2026-04-26). RESOLVED. `playwright install chromium` was failing inside the container due to missing shared libraries. Fix: Dockerfile now installs `libnss3`, `xvfb`, and other Chromium system deps at image build time. Chromium is baked into the image at `/root/.cache/ms-playwright/`.
- Anna's Archive downloads CAPTCHA — old host-CDP approach (2026-04-16). OBSOLETE. Originally used host CDP on port 9222 with xpra. Replaced by container-local Playwright (see 2026-04-26). Host CDP dependency removed.
- **Old VNC/noVNC approach (2026-04-27).** OBSOLETE. The old manual x11vnc+websockify hack (ports 5998/5999, host display :99) was a desperate workaround that never worked. Now superseded by the clean Dockerfile-baked stack below.
- **xpra 3.1 HTML5 display stack (2026-04-30).** OBSOLETE. Entire xpra approach replaced with x11vnc+websockify+noVNC. See Red Herring Graveyard below for why.

## Known Issues — CURRENT

- **Anna's Archive title extraction:** The parser extracts MD5 hashes correctly but titles show as "Unknown" — the title lives in a complex nested DOM structure that needs further parsing work.
- **Jackett seeder counts via Torznab:** Consistently report 0 seeders even when torrents are alive. TPB's Torznab adapter doesn't report accurate seeder data.

**SOP for Anna's Archive downloads (updated 2026-04-30):**
1. Automation first — `browser_download()` navigates book page, identifies and clicks the best download candidate
2. If DDoS-Guard JS challenge: wait up to 30s for auto-redirect
3. If visual puzzle or hCAPTCHA: container returns `screenshot_b64` + `display_url`
4. Human-in-the-loop URL: `https://araminta.taild3f7b9.ts.net/pirate/vnc_lite.html?path=pirate%2F`
5. After challenge resolves → countdown page → `_handle_countdown_and_extract()` polls up to 180s
6. Token URL pattern: `https://wbsg8v.xyz/d3/y/{unix_ts}/3000/g4/{category}/...`
7. File curl'd to `/downloads` with proper cookies and headers

---

## 🔴 Red Herring Graveyard

**These approaches were explored and FAILED. Do NOT attempt again.**

### XPRA (all approaches) — DO NOT RETRY
- **What was tried:** xpra 3.1 (Ubuntu 22.04 apt package) in both `shadow` and `start` modes. xpra pip upgrade attempted and failed (needs full Cython build chain).
- **How it failed:** jQuery was a symlink (→ `/usr/share/javascript/jquery/jquery.js`) which xpra's built-in HTTP server doesn't follow — returned 404. Even after resolving the symlink by installing `libjs-jquery` and copying the real file inline, xpra's application-layer WebSocket handshake threw "server error error accepting new connection" on every HTML5 client attempt. The raw WebSocket upgrade (101) worked at the TCP level, but xpra's own protocol handshake after upgrade was broken. Both shadow and start modes failed identically.
- **Why we thought it would work:** Previous sessions had used xpra's HTML5 client successfully with CDP-based flows. The xpra documentation claims HTML5 support.
- **Signs it was a dead end:** Same error across multiple restarts, both display modes, even after jQuery fix. No amount of configuration flags changed the outcome.
- **What actually works:** x11vnc + websockify + noVNC (see below).

### Manual x11vnc + websockify (April 27 hack) — DO NOT RETRY
- **What was tried:** Manually launching x11vnc and websockify from inside the container without Dockerfile integration, using port 5901 and noVNC's `vnc.html`.
- **How it failed:** `vnc.html` doesn't handle path-based WebSocket routing (needs `vnc_lite.html`). Port 5901 wasn't whitelisted in NordVPN killswitch. No persistence on rebuild.
- **What actually works:** `vnc_lite.html?path=pirate%2F` with everything Dockerfile-baked.

### Host CDP / browser on host — DO NOT RETRY
- **What was tried:** Running Chromium on the host Pi with CDP on port 9222, routing through container VPN.
- **How it failed:** Traffic leaked outside VPN. Host ISP DNS filtering blocks Anna's Archive. Violates the "all naughty traffic inside container" principle.

---

## Architecture Principles (never violate)

### Lessons learned (2026-04-30)
**Display stack:** The canonical browser display is `Xvfb :1 → x11vnc :5900 → websockify :6081 → noVNC`. xpra 3.1 is broken for HTML5 WebSocket — see Red Herring Graveyard. The `path=pirate%2F` URL parameter is MANDATORY for Funnel routing.

**Self-contained images:** Docker images must contain real files, not symlinks to files outside their document root. xpra's jquery.js symlink was the original red herring that wasted hours.

**Do not put LLM/vision providers or API keys inside container tools.** The container cannot import agent capabilities, and it should not call OpenRouter/OpenAI/Gemini/etc. directly. Correct boundary: `browser_fallback.py` returns screenshots and the live display URL; Minty/the calling agent decides whether to use its own vision capability or send David the noVNC link.

1. **All VPN traffic originates from INSIDE the container.** Never install/run NordVPN on the host Pi.
2. **Container is disposable:** `docker compose down && up` should restore everything.
3. **Host-to-container ports:** API and Jackett stay localhost-only (`127.0.0.1:9876`, `127.0.0.1:9118`). noVNC/websockify display is published as host port `6081` because Tailscale Funnel proxies it to the Browser URL.
4. **Auto-whitelist Docker bridge subnet** in NordVPN on startup so killswitch doesn't block host access.
5. **If you need to interact with the browser from within the VPN tunnel, use the noVNC URL** (`https://araminta.taild3f7b9.ts.net/pirate/vnc_lite.html?path=pirate%2F`) — never run a browser on the host and route through the container.

---

## Notes

- Downloads land in `/downloads` inside the container, mapped to `./downloads` on the host (bind mount)
- Jackett state persisted in `pirate-dock-data` Docker volume at `/data/jackett/`
- Build scripts include disk space guardrails (refuses to build above 85% usage)
- `.dockerignore` prevents build context bloat (no .git, downloads, docs inside image)
- Image is ~1.5 GB with Playwright + Chromium baked in (was ~400 MB before browser fallback)
- VPN kill switch blocks non-VPN traffic INSIDE the container — this is correct and desired. Host networking is unaffected.
- Token is 64 chars, stored in `.env` and `scripts/token.txt` — never commit `token.txt` to git
- **Browser stack runs INSIDE the container** via Playwright; no host CDP or noVNC server required
- **Network mode** is bridge (NOT host) — ports 9876 and 9118 published to `127.0.0.1` only, port 6081 published to `0.0.0.0` for Tailscale Funnel
- **DO NOT change to `network_mode: host`** — this would re-introduce the 2026-04-17 incident where NordVPN's killswitch broke all Pi connectivity
- **Playwright requirements:** `playwright>=1.50.0` in `requirements.txt`; Dockerfile installs Chromium libs + runs `playwright install chromium`
- **noVNC display URL:** `https://araminta.taild3f7b9.ts.net/pirate/vnc_lite.html?path=pirate%2F` — the `path` parameter is mandatory
