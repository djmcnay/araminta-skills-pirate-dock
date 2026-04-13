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
curl -s http://localhost:9876/status | python3 -m json.tool
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

**Note:** Without a paid API key, downloads may need CAPTCHA.
Search works fine without authentication.

## Workflow: Torrent Search (Jackett)

1. Jackett runs inside the container with 50+ indexer support
2. First run: configure indexers via web UI at `http://localhost:9118`
3. Search: `GET /search/torrents?q=UFC+327`
4. Returns results with title, size, seeders, magnet link
5. Download: `POST /download/magnet {"magnet": "magnet:..."}`

## Workflow: UFC Event Watch

1. `POST /watch/ufc {"event": "UFC 327", "quality": "1080"}`
2. Background poller searches all indexers every 5 min
3. Filters: event name match + quality (1080p) + seeders >= 2
4. Check status: `GET /watch/ufc`
5. When found: `POST /download/magnet` with best match
6. Stop watching: `DELETE /watch/ufc/ufc_327`

---

## Credentials & Config

- **NORDVPN_TOKEN:** In `~/Documents/GitHub/pirate-dock/.env`
- **Jackett API key:** Auto-detected on startup from `/data/jackett/appsettings.json`
- **Jackett config:** Via web UI at `http://localhost:9118` (inside container)
- **NordVPN default region:** South Africa (NordLynx P2P)

---

## Notes

- Downloads land in `/downloads` inside the container, mapped to `./downloads` on the host
- Jackett state persisted in `pirate-dock-data` Docker volume at `/data/jackett/`
- Build scripts include disk space guardrails (refuses to build above 85% usage)
- `.dockerignore` prevents build context bloat (no .git, downloads, docs inside image)
- The old `pirate-container` (Node.js based) has been archived — do not use
- Image is ~400MB (no Chromium!) vs old 2.77GB
