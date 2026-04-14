# Pirate Dock

A bespoke Docker container for privacy-first downloads — ebooks from Anna's Archive and torrents via Jackett, all tunnelled through NordVPN.

## Architecture (v2)

**No browser. No Playwright. No Chromium.** Just HTTP APIs, aria2, and a VPN tunnel.

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

**Key design decisions:**
- All traffic through **NordVPN South Africa** (NordLynx P2P) via a strict kill switch
- No external browser services (Browserbase, Steel, Camoufox) — keeps traffic inside the VPN
- Anna's Archive: search works, downloads blocked by DDoS-Guard CAPTCHA (user clicks link manually)
- Torrents: full pipeline search → magnet → aria2 download behind VPN

## Quick Start

```bash
cd ~/Documents/GitHub/pirate-dock
# Make sure .env has NORDVPN_TOKEN
bash scripts/build.sh          # Safe build with disk check
```

The container exposes:
- **FastAPI API:** `http://localhost:9876` (use `docker exec` inside container)
- **Jackett UI:** `http://localhost:9118`

## API Reference (`http://localhost:9876`)

*Note: Use `docker exec pirate-dock curl http://127.0.0.1:9876/...` if VPN kill switch blocks host access.*

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

**Note:** Anna's Archive search returns MD5 hashes and metadata. Slow/fast downloads are blocked by DDoS-Guard CAPTCHA — provide the user the page link to click manually.

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
| POST | `/download/magnet` | Download via aria2: `{"magnet": "..."}` |
| GET | `/downloads/active` | Running aria2 processes |
| GET | `/downloads/list` | Files in /downloads |

### UFC Watch (background poller)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/watch/ufc` | Start watching: `{"event": "UFC 327"}` |
| GET | `/watch/ufc` | Status of all watches |
| DELETE | `/watch/ufc/{key}` | Stop watching |

## Workflows

### Book Request

1. Search both pipelines in parallel:
   - `GET /search/annas-archive?q=<title+author+isbn>`
   - `GET /search/torrents?q=<title+author+isbn>`
2. **Torrent found?** → auto-download via `POST /download/magnet`
3. **No torrent?** → provide the Anna's Archive page link for manual download
4. Report: title, MD5, size, format, download status

### Torrent / UFC Workflow

1. Jackett runs inside the container, configured via `/data/jackett/`
2. Search via API: `GET /search/torrents?q=UFC+327`
3. Pick a result, download: `POST /download/magnet` with the magnet link
4. Or use `/watch/ufc` to poll automatically for a new event

## Skill Tests

The `scripts/test.py` file provides a test suite that validates all major functionality:

```bash
# Run inside the container
docker exec pirate-dock python3 /app/scripts/test.py

# Or from the host
cd ~/Documents/GitHub/pirate-dock
python3 scripts/test.py
```

**Tests:**
1. **Anna's Archive** — searches for "Japaneasy Kitchen" by Tim Anderson, verifies results and generates download links
2. **UFC Video Search** — searches Jackett for "UFC", displays top 10 results with sizes and sources
3. **Top Gun Lifecycle** — full torrent lifecycle: search → start download → cancel → delete partial files → verify clean

## Files

```
├── Dockerfile              # Based on bubuntux/nordvpn (~400MB)
├── docker-compose.yml      # VPN + Jackett + API ports
├── .dockerignore           # Prevents build context bloat
├── .env                    # NORDVPN_TOKEN (gitignored)
├── README.md               # This file
├── SKILL.md                # Agent skill documentation
├── scripts/
│   ├── server.py           # FastAPI server (all endpoints)
│   ├── test.py             # Skill test suite
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

# View container logs
docker logs pirate-dock --tail 50
```

## Changelog

### 2026-04-14 — Bug fixes and Anna's Archive parser update

- **Fixed:** Anna's Archive search URL (`/s?q=` → `/search?q=`) — AA changed their URL structure
- **Fixed:** Anna's Archive HTML parser — new UI uses `.js-aarecord-list-outer` container instead of `.js-search-result` divs
- **Fixed:** Jackett startup deadlock — `_start_jackett()` now checks for already-running Jackett before starting a new process; accepts both HTTP 200 and 302 responses (Jackett returns 302 for the indexers endpoint)
- **Added:** `scripts/test.py` — three-test suite covering Anna's Archive search, Jackett torrent search, and full download lifecycle
- **Added:** Skill test documentation
- **Updated:** SKILL.md with corrected workflow, parser details, and known issues

## Notes

- Downloads land in `/downloads` inside the container, mapped to `./downloads` on the host
- Jackett state persisted in `pirate-dock-data` Docker volume at `/data/jackett/`
- Image is ~400MB (no Chromium!) vs old 2.77GB
- VPN kill switch blocks non-VPN traffic — use `docker exec` for local API calls from the host
- Token stored in `.env` and `scripts/token.txt` — **never commit `token.txt`** (in `.gitignore`)
- Seeder counts reported via Torznab may show 0 even when torrents are alive — try downloading to verify
