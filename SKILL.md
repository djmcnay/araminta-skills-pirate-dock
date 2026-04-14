---
name: pirate-dock
description: Control Pirate Dock — a bespoke Docker container for VPN-protected ebook downloads (Anna's Archive) and torrent search (Jackett). All traffic routed through NordVPN South Africa P2P.
category: media
---

# Pirate Dock Skill

## Overview
A custom-built Docker container running NordVPN (South Africa, P2P), aria2 for downloads, and Jackett for multi-site torrent search. Two primary workflows: downloading eBooks from Anna's Archive, and searching/downloading torrents (UFC focus).

**Architecture:** NO browser. NO Playwright. Just HTTP APIs + aria2.
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

## Workflow: eBook from Anna's Archive

1. User provides book name, ISBN, or MD5 hash
2. If name: `GET /search/annas-archive?q=Book+Name`
3. Returns list with MD5 hashes, titles, sources
4. Pick result: `GET /download/annas-archive/{md5}`
5. Returns page URL, title, and any direct download links
6. Files land in `/downloads` → copy to `araminta-vault/library/`

**Download reality check (as of 2026-04-14):**
- **Search always works** — scraping Anna's Archive search results page is reliable
- **Slow downloads blocked** — Anna's Archive uses DDoS-Guard browser challenge on download endpoints. Cannot bypass from within the container (no browser). Need either a browser-based bypass or the DDoS-Guard to be removed.
- **Internet Archive (IA) downloads** — some books require borrow authorization (401). Openly available IA items download fine.
- **Torrent via Anna's Archive** — torrents on aa are bulk archive torrents (200GB+), not individual books
- **Best current path:** Jackett torrent search → magnet → aria2. Requires configured indexers.

## Workflow: Torrent Search (Jackett)

1. Jackett runs inside the container with 619 indexer definitions loaded
2. **FIRST RUN: configure indexers via web UI at `http://localhost:9118`**
   - Must be done from a browser (not via API)
   - Public indexers (The Pirate Bay, 1337x, etc.) need to be explicitly enabled
   - Private indexers need account credentials
3. Search: `GET /search/torrents?q=UFC+327`
4. Returns results with title, size, seeders, magnet link
5. Download: `POST /download/magnet {"magnet": "magnet:..."}`

**Jackett setup priority:** Enable The Pirate Bay and 1337x as a minimum. These are public, no account needed. Access the Jackett web UI via `http://localhost:9118` from a machine that can reach the Pi.

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

---

## Known Issue — RESOLVED (2026-04-14)

**VPN login bug: FIXED.** The s6-overlay init system in the bubuntux/nordvpn image strips Docker environment variables from CMD services. `nordvpn login --token` was receiving an empty token.
**Fix:** Token is now read from a bind-mounted file (`/run/pirate-dock/token`) instead of environment variables. The `docker-compose.yml` maps `./scripts/token.txt` to `/run/pirate-dock/token` (read-only). A fallback to env vars is included for resilience.

**How it works now:**
1. `scripts/token.txt` contains the NordVPN token (loaded from `.env` at build time)
2. `scripts/run.sh` reads the token from the bind-mounted file
3. The s6 init system starts the NordVPN daemon (nordvpnd) and firewall as normal
4. `run.sh` does `nordvpn login --token` with the file-sourced token, then connects
5. Jackett starts alongside, and the FastAPI server serves on 9876

**Config:**
- `docker-compose.yml` uses `command: ["/bin/bash", "/app/scripts/run.sh"]` (not `entrypoint` — preserves s6 `/init`)
- Token file: `./scripts/token.txt` (64 chars, loaded from `.env` via `grep`)
- To update token: edit `.env`, then `grep NORDVPN_TOKEN .env | cut -d= -f2 > scripts/token.txt`

## Known Issue — CURRENT (2026-04-14)

**Jackett returns 0 search results.** 619 indexer definitions are loaded, but no public indexers are enabled by default. Must configure indexers via Jackett web UI (`http://localhost:9118`) before torrent search works. Minimum setup: enable The Pirate Bay and 1337x (public, no account needed).

**Anna's Archive downloads blocked by DDoS-Guard.** The download endpoints (`/slow_download/`, `/fast_download/`) use DDoS-Guard browser challenge. The container has no browser. The metadata/search page on the `.pk` mirror works fine; only the download endpoints are protected. Alternative: configure Jackett indexers for direct torrent download instead.

## Notes

- Downloads land in `/downloads` inside the container, mapped to `./downloads` on the host
- Jackett state persisted in `pirate-dock-data` Docker volume at `/data/jackett/`
- Build scripts include disk space guardrails (refuses to build above 85% usage)
- `.dockerignore` prevents build context bloat (no .git, downloads, docs inside image)
- The old `pirate-container` (Node.js based) has been archived — do not use
- Image is ~400MB (no Chromium!) vs old 2.77GB
- VPN kill switch blocks non-VPN traffic — use `docker exec` for local API calls from the host
- Token is 64 chars, stored in `.env` and `scripts/token.txt` — never commit `token.txt` to git (it's in `.gitignore`)
