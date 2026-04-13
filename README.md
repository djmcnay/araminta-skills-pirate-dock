# Pirate Dock

A bespoke Docker container for VPN-protected downloads — ebooks from Anna's Archive and torrents via Jackett.

## Architecture (v2)

**No browser. No Playwright. No Chromium.** Just HTTP APIs.

```
┌─────────────────────────────────────────────┐
│  pirate-dock container (NordVPN tunnel)     │
│                                             │
│  ┌─────────┐    ┌─────────┐    ┌─────────┐ │
│  │ FastAPI  │───▶│ Jackett │    │  aria2  │ │
│  │ :9876    │    │ :9118   │    │(download│ │
│  └─────────┘    └─────────┘    └─────────┘ │
│       │              │              │       │
│       ▼              ▼              ▼       │
│  Anna's Archive   50+ torrent    /downloads │
│  (HTTP scrape)    indexers       (PDF/ePUB/ │
│                   (Torznab)       video)    │
└─────────────────────────────────────────────┘
```

## Quick Start

```bash
cd ~/Documents/GitHub/pirate-dock
# Make sure .env has NORDVPN_TOKEN
bash scripts/build.sh          # Safe build with disk check
```

## API Reference (`http://localhost:9876`)

### VPN

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/status` | VPN + Jackett status |
| POST | `/vpn/connect` | `{"country": "South_Africa"}` |
| POST | `/vpn/disconnect` | Disconnect VPN |

### Anna's Archive (eBooks)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET/POST | `/search/annas-archive?q=...` | Search books by title/author/ISBN |
| GET | `/download/annas-archive/{md5}` | Get download info by MD5 hash |
| POST | `/download/annas-archive` | Download by MD5: `{"md5": "..."}` |

### Torrent Search (via Jackett)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/search/torrents?q=...` | Search all configured indexers |
| GET | `/search/piratebay?q=...` | Search PirateBay only |
| GET | `/search/1337x?q=...` | Search 1337x only |
| GET | `/search/ext?q=...` | Search ext.to only |
| GET | `/jackett/indexers` | List available indexers |

### Downloads

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/download/magnet` | Download via magnet: `{"magnet": "..."}` |
| GET | `/downloads/active` | Running aria2 processes |
| GET | `/downloads/list` | Files in /downloads |

### UFC Watch (background poller)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/watch/ufc` | Start watching: `{"event": "UFC 327"}` |
| GET | `/watch/ufc` | Status of all watches |
| DELETE | `/watch/ufc/{key}` | Stop watching |

## eBook Workflow

1. Search: `GET /search/annas-archive?q=Project+Hail+Mary`
2. Pick a result from the list (MD5 + title + source)
3. Download: `GET /download/annas-archive/{md5}`
4. Files land in `/downloads`, copy to `araminta-vault/library/`

**Note:** Without a paid Anna's Archive API key, direct downloads
may require CAPTCHA on the slow servers. The search endpoint gives
you MD5 hashes and page URLs. For bulk/automated downloads, consider
downloading the pilimi metadata index (see Plan doc).

## Torrent / UFC Workflow

1. Jackett runs inside the container, configured via `/data/jackett/`
2. Access Jackett web UI at `http://localhost:9118` (first run: set up indexers)
3. Search via pirate-dock API: `GET /search/torrents?q=UFC+327`
4. Pick a result, download: `POST /download/magnet` with the magnet link
5. Or use `/watch/ufc` to poll automatically for a new event

## Files

```
├── Dockerfile              # Based on bubuntux/nordvpn
├── docker-compose.yml      # VPN + Jackett ports exposed
├── .dockerignore           # Prevents build context bloat
├── scripts/
│   ├── server.py           # FastAPI server (all endpoints)
│   ├── entrypoint.sh       # Container startup
│   ├── requirements.txt    # Python deps
│   ├── build.sh            # Safe build wrapper
│   ├── pre-build-check.sh  # Disk space guardrail
│   └── prune-docker.sh     # Cleanup helper
└── downloads/              # Downloaded files (bind mount)
```

## Maintenance

```bash
# Safe rebuild (checks disk space first)
bash scripts/build.sh

# Clean up Docker junk
bash scripts/prune-docker.sh

# Nuclear cleanup (keeps running containers)
bash scripts/prune-docker.sh --aggressive
```
