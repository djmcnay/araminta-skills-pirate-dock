# Pirate Dock — Implementation Plan

## Objective
A bespoke Docker container for two workflows:
1. **eBook downloading** — Anna's Archive (search by title/ISBN, download by MD5)
2. **UFC torrents** — Jackett-powered multi-site search, aria2 download behind VPN

## Architecture v2 (2026-04-13)

**Key change: NO browser.** Removed Playwright/Chromium entirely. Image went from 2.77GB to ~400MB.

```
pirate-dock container (NordVPN tunnel)
├── FastAPI server (:9876) — all endpoints
├── Jackett (:9118) — torrent indexer proxy (50+ sites)
└── aria2 — download engine
```

**Why this works:**
- Anna's Archive provides an LLMs.txt explaining programmatic access
- All torrents/metadata available via JSON API
- Torrent sites aggregated by Jackett (Torznab API)
- Cloudflare-protected sites handled by Jackett internally
- Search by MD5 hash → construct download URL → aria2 downloads

---

## Phase 1: Docker Foundation ✅

**Status:** Complete. Container builds and runs.

- [x] Project at `~/Documents/GitHub/pirate-dock/`
- [x] Base image: `ghcr.io/bubuntux/nordvpn:latest` (proven on Pi)
- [x] Python 3 + FastAPI + httpx + aria2
- [x] **Jackett binary** added to image (multi-arch: amd64/arm64/armv7)
- [x] **No Playwright, no Chromium** — saves ~800MB+ in image
- [x] FastAPI server with all endpoints (VPN, search, download)
- [x] Entrypoint: VPN daemon + Jackett + uvicorn
- [x] Safe build scripts with disk space guardrails
- [x] `.dockerignore` prevents build context bloat
- [x] SKILL.md updated for Minty

## Phase 2: Anna's Archive (eBooks) — IN PROGRESS

**Status:** Search and download endpoints implemented. Awaiting build test.

- [x] `/search/annas-archive?q=...` — scrapes search results, extracts MD5 hashes
- [x] `/download/annas-archive/{md5}` — fetches book page, extracts info
- [x] Multiple mirror support (`.gl`, `.pk`, `.gd`) with automatic fallback
- [ ] **Test:** Build container and verify search works
- [ ] **Test:** Verify download flow end-to-end
- [ ] **Optional:** Metadata index for offline search (see below)

### Optional: Offline Metadata Index

Anna's Archive provides bulk metadata torrents for programmatic search:
- `pilimi-zlib-index-2022-06-28` — 1.6GB (core Z-Library index)
- `pilimi-zlib2-index-2022-08-24` — 2.5GB (second wave)
- `pilimi-zlib2-derived` — 0.7GB (cleaned metadata)

Total: ~5GB compressed. Contains title, author, MD5, ISBN for millions of books.
Stored in `/data/metadata/` volume. Searchable offline without hitting Anna's Archive.

**If needed:** Download the three pilimi torrents into `/data/metadata/`,
decompress, and index with a lightweight search engine (SQLite FTS5 or Whoosh).

## Phase 3: Torrent / UFC (Video)

**Status:** Implemented via Jackett API. Awaiting build test.

- [x] Jackett runs as subprocess, auto-starts on container boot
- [x] `/search/torrents?q=...` — searches all configured Jackett indexers
- [x] `/search/piratebay`, `/search/1337x`, `/search/ext` — legacy endpoints, routed through Jackett
- [x] `/download/magnet` — starts aria2 download
- [x] `/watch/ufc` — background poller for UFC events (quality filter + seeder threshold)
- [x] VPN check enforced before any download
- [ ] **Test:** Build and verify torrent search
- [ ] **Configure Jackett:** Set up indexers via web UI (`:9118`)

### UFC Watch Workflow
1. `POST /watch/ufc {"event": "UFC 327", "quality": "1080"}` — starts polling
2. Background task searches all indexers every 5 minutes
3. Filters by event name + quality (1080p) + seeders >= 2
4. Stores best match — check status with `GET /watch/ufc`
5. When found: `POST /download/magnet` with the best match

---

## Known Pitfalls

### VPN
- NordVPN in Docker requires `NET_ADMIN` + `SYS_MODULE` capabilities and `/dev/net/tun`
- `HOME` must be `/root` or NordVPN CLI fails silently
- WireGuard kernel module must be loaded on Pi host: `sudo modprobe wireguard`
- Country names use underscores: `South_Africa` not `South Africa`
- bubuntux/nordvpn base image is battle-tested on this Pi — keep it

### Disk
- Pi NVMe: 117GB total. Guardrails prevent builds above 85% disk usage.
- Old image was 2.77GB; new image should be ~400MB (no Chromium)
- Metadata index (if downloaded) needs ~5GB + decompression space

### Anna's Archive
- `.gl` may go down — always try multiple mirrors
- CAPTCHA blocks direct download without API key — search works fine
- The LLMs.txt (https://annas-archive.gl/blog/llms-txt.html) documents all programmatic access

### Jackett
- First launch creates default config at `/data/jackett/`
- Must configure indexers via web UI (port 9118) on first run
- API key auto-detected from appsettings.json on startup
- Some indexers require VPN to be connected first

## Session History

### 2026-04-12 — Phase 1 scaffolded
- Project created, Dockerfile with Playwright (now removed)
- NordVPN base image working, VPN connects to South Africa
- WireGuard module needed on Pi host

### 2026-04-13 — Architecture v2 (this revision)
- Discovered Anna's Archive LLMs.txt — no browser needed
- Removed Playwright/Chromium entirely
- Added Jackett for multi-site torrent search
- New server.py with all endpoints implemented
- Safe build scripts + disk guardrails added
- Old images cleaned up (2.77GB reclaimed)
