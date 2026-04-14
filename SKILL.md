---
name: pirate-dock
description: Control Pirate Dock — a bespoke Docker container for VPN-protected ebook and media downloads. Searches Anna's Archive and torrent indexers behind NordVPN. Auto-downloads via torrent when possible, sends manual download links when automation can't bypass CAPTCHAs.
category: media
---

# Pirate Dock Skill

## Overview
A custom-built Docker container running NordVPN (South Africa, P2P), aria2 for downloads, and Jackett for multi-site torrent search. Two parallel pipelines: Anna's Archive for ebooks, Jackett for torrents/video.

**Architecture:** NO browser. NO Playwright. Just HTTP APIs + aria2 + VPN tunnel. All search happens behind NordVPN; if automation can't complete a download, send the user the direct link to click manually.
**Project home:** `~/Documents/GitHub/pirate-dock/`
**Container name:** `pirate-dock`
**API:** `http://localhost:9876`
**Jackett UI:** `http://localhost:9118`

---

## Container Management

### Build & start
```bash
cd ~/Documents/GitHub/pirate-dock
# ALWAYS use the safe build script (checks disk space)
bash scripts/build.sh
```

### Check status
```bash
docker exec pirate-dock curl -sf http://127.0.0.1:9876/status | python3 -m json.tool
```
Note: External `localhost:9876` may be blocked by VPN kill switch — use `docker exec` instead.

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

---

## API Reference (`http://localhost:9876`)

*Note: Use `docker exec pirate-dock curl http://127.0.0.1:9876/...` if VPN kill switch blocks host access.*

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
| POST | `/search/annas-archive` | `{"query": "..."}` | Search (POST version) |
| GET | `/download/annas-archive/{md5}` | — | Get download info by MD5 |
| POST | `/download/annas-archive` | `{"md5": "..."}` | Download by MD5 hash |

### Torrent Search (via Jackett)

| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| GET | `/search/torrents?q=...` | — | Search all configured indexers |
| GET | `/search/piratebay?q=...` | — | Search PirateBay only |
| GET | `/search/1337x?q=...` | — | Search 1337x only |
| GET | `/search/ext?q=...` | — | Search ext.to only |
| GET | `/jackett/indexers` | — | List available Jackett indexers |

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

## Core Workflow: Book Request (2026-04-14)

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
- User clicks the link and completes the download manually (one-click behind the VPN)

**C) Nothing found on either pipeline → report that clearly**

### Step 4: Report results

Always provide:
1. **What the book is** (title, author, format, size)
2. **Auto-download status** (downloaded / link to click)
3. **Anna's Archive link** (always include, as fallback)
4. **Torrent details** (if applicable: seeders, source indexer)

---

## Download Reality (as of 2026-04-14)

**What works automatically:**
- ✅ Anna's Archive **search** — reliable scraping of search results
- ✅ Jackett **torrent search** — TPB, 1337x, LimeTorrents, YTS, EZTV configured
- ✅ **aria2 downloads** via magnet — fast when seeders are healthy

**What doesn't work automatically:**
- ❌ Anna's Archive **slow/fast downloads** — DDoS-Guard browser challenge blocks all automated access. Requires manual browser interaction with a proprietary CAPTCHA. No third-party service (Browserbase, Camoufox, Steel) can bypass it reliably.

**Why no browser automation:** The container is deliberately lightweight (~400MB) and VPN-tunnelled. Outsourcing downloads to third-party browser services (Browserbase, Steel) would route traffic outside the VPN, defeating the privacy model. The architecture is: **container searches, container downloads if possible, user downloads manually if not.**

---

## Workflow: Torrent Search (Jackett)

1. Jackett runs inside the container with 619 indexer definitions loaded
2. **Configured indexers:** The Pirate Bay, 1337x, LimeTorrents, YTS, EZTV, 1337x (public, no account needed)
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

## Known Issue — RESOLVED

**VPN login bug: FIXED (2026-04-14).** Token read from bind-mounted file (`/run/pirate-dock/token`) instead of env vars (s6-overlay strips env vars).

**Jackett indexers: CONFIGURED (2026-04-14).** TPB, 1337x, LimeTorrents, YTS, EZTV all enabled and returning results.

## Known Issue — CURRENT

**Anna's Archive downloads: CAPTCHA-GATED (ongoing).** DDoS-Guard on `/slow_download/` and `/fast_download/` requires manual browser verification (proprietary CAPTCHA). Automated download not possible without violating the privacy architecture (third-party browser services outside VPN). Solution: provide the user with the direct Anna's Archive link for manual download.

---

## Notes

- Downloads land in `/downloads` inside the container, mapped to `./downloads` on the host
- Jackett state persisted in `pirate-dock-data` Docker volume at `/data/jackett/`
- Build scripts include disk space guardrails (refuses to build above 85% usage)
- `.dockerignore` prevents build context bloat (no .git, downloads, docs inside image)
- Image is ~400MB (no Chromium!) vs old 2.77GB
- VPN kill switch blocks non-VPN traffic — use `docker exec` for local API calls from the host
- Token is 64 chars, stored in `.env` and `scripts/token.txt` — never commit `token.txt` to git (it's in `.gitignore`)
