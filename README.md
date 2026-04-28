# Pirate Dock

A bespoke Docker container for privacy-first downloads — ebooks from Anna's Archive and torrents via Jackett, all tunnelled through NordVPN.

## Architecture (v3)

Pirate Dock keeps browser automation and all download/search traffic inside the
container's NordVPN network namespace. The host Pi does not join the VPN. When
Minty needs human help, she shows the container browser through Xpra over
Tailscale.

```
┌─────────────────────────────────────────────┐
│  pirate-dock container (NordVPN tunnel)     │
│                                             │
│  ┌─────────┐    ┌─────────┐    ┌─────────┐ │
│  │ FastAPI │───▶│ Jackett │    │ aria2   │ │
│  │ :9876   │    │ :9118   │    │download │ │
│  └────┬────┘    └─────────┘    └─────────┘ │
│       │                                     │
│       ▼                                     │
│  Playwright/Chromium on Xvfb :1             │
│  Xpra HTML5 display on :6081                │
└─────────────────────────────────────────────┘

Tailnet URL for human-in-the-loop browser access:
https://araminta.taild3f7b9.ts.net:8443/pirate/
```

**Key design decisions:**
- All traffic through **NordVPN South Africa** (NordLynx P2P) via a strict kill switch
- Browser fallback runs **inside the container**, never on the host
- Tailscale Serve exposes the Xpra display to the Tailnet only:
  `https://araminta.taild3f7b9.ts.net:8443/pirate/` → `http://127.0.0.1:6081`
- Minty can send that URL by WhatsApp when CAPTCHA, login, or visual confirmation blocks automation
- Torrents: full pipeline search → magnet → aria2 download behind VPN

## Quick Start

```bash
cd ~/Documents/GitHub/pirate-dock
# Make sure .env has NORDVPN_TOKEN
bash scripts/build.sh          # Safe build with disk check
```

The container exposes:
- **FastAPI API:** `http://localhost:9876`
- **Jackett UI:** `http://localhost:9118`
- **Human browser display:** `https://araminta.taild3f7b9.ts.net:8443/pirate/` (Tailnet only)

Tailscale Serve must point at Xpra:

```bash
sudo tailscale serve --bg --https=8443 --set-path=/pirate 6081
sudo tailscale serve status
# expected:
# https://araminta.taild3f7b9.ts.net:8443 (tailnet only)
# |-- /pirate proxy http://127.0.0.1:6081
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
| POST | `/download/annas-archive/browser` | Force container browser fallback: `{"md5": "..."}` |
| GET | `/download/annas-archive/{md5}/browser` | Force container browser fallback |
| GET | `/browser/status` | Check Playwright and the display URL |

**Note:** Anna's Archive search returns MD5 hashes and metadata. If DDoS-Guard
or CAPTCHA blocks automation, Minty should send
`https://araminta.taild3f7b9.ts.net:8443/pirate/` so the user can interact with the browser
running inside the VPN container.

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
cd ~/Documents/GitHub/pirate-dock
python3 scripts/test.py
python3 scripts/test_browser.py
python3 scripts/test_isolation.py
```

**Tests:**
1. **Anna's Archive** — searches for "Japaneasy Kitchen" by Tim Anderson, verifies results and generates download links
2. **UFC Video Search** — searches Jackett for "UFC", displays top 10 results with sizes and sources
3. **Top Gun Lifecycle** — full torrent lifecycle: search → start download → cancel → delete partial files → verify clean

## Files

```
├── Dockerfile              # NordVPN base plus Playwright/Chromium/Xpra
├── docker-compose.yml      # VPN + Jackett + API + Xpra ports
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
- Image includes Chromium/Xpra for human-in-the-loop browser fallback
- VPN kill switch applies inside the container; host access works through whitelisted published ports
- Token stored in `.env` and `scripts/token.txt` — **never commit `token.txt`** (in `.gitignore`)
- Seeder counts reported via Torznab may show 0 even when torrents are alive — try downloading to verify
