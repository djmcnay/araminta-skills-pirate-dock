# Pirate Dock v3.2 — Display Fix Rebuild

**Started:** 2026-04-30 ~09:00
**Completed:** 2026-04-30 ~14:55
**Session ref:** CLI, deepseek-v4-pro:cloud
**Goal:** Rebuild container with self-contained x11vnc + noVNC + websockify display stack. Abandon xpra entirely.

---

## Diagnosis (2026-04-30, pre-rebuild)

**Original container state:**
- Container UP 10 hours, NordVPN connected (Johannesburg, NordLynx)
- API on :9876 working
- Jackett: NOT RUNNING

**Display stack — Frankenstein:**
- Xvfb :1 — running ✓
- xpra shadow :1 — ZOMBIE (crashed, `<defunct>`)
- x11vnc on localhost:5901 — running (manually added, NOT in run.sh/Dockerfile)
- websockify on 0.0.0.0:6081 → localhost:5901 — running (manually added, NOT in run.sh/Dockerfile)
- noVNC served at /pirate/vnc.html — loads but "Failed to connect to server"
- Chromium — running headed on :1

**Root cause: xpra 3.1 fundamentally broken on Ubuntu 22.04**
After multiple failed attempts (jQuery path fixes, symlink resolution, libjs-jquery installation), xpra 3.1's HTML5 WebSocket handshake fails at the application layer with "error accepting new connection" despite a successful 101 Switching Protocols upgrade. This is an xpra 3.1 bug, not a configuration issue.

**Decision:** Abandon xpra. Switch to x11vnc + noVNC + websockify.

---

## Red Herring Graveyard

These were attempted and failed before the correct solution was found. Do NOT revisit.

1. **xpra 3.1 (Ubuntu 22.04 apt)** — jQuery path mismatch (js/lib/ vs js/), then symlink resolution, then WebSocket handshake failure at application layer. Fundamentally broken for HTML5.
2. **xpra from pip** — Requires build dependencies not available in the base NordVPN image.
3. **Manual x11vnc + websockify in running container** — Worked for testing but was NOT baked into Dockerfile/run.sh. Would not survive rebuild.
4. **Symlink-based fixes for jQuery** — xpra's built-in HTTP server doesn't follow symlinks.
5. **vnc.html (standard noVNC)** — Does NOT handle the `path` parameter correctly for Tailscale Funnel routing. Must use `vnc_lite.html`.

---

## Final Working Architecture

```
Xvfb :1 (virtual framebuffer, 1280x800x24)
    ↓
x11vnc (exports :1 as VNC on localhost:5900, -localhost -shared -nopw)
    ↓
websockify (bridges VNC→WebSocket on 0.0.0.0:6081, serves noVNC from /usr/share/novnc)
    ↓
Tailscale Funnel: /pirate → :6081
    ↓
David opens: https://araminta.taild3f7b9.ts.net/pirate/vnc_lite.html?path=pirate%2F
```

**Browser:** Playwright Chromium 1217 at `/root/.cache/ms-playwright/chromium-1217/chrome-linux/chrome`
- Requires `--no-sandbox` inside Docker
- Camoufox Firefox also available

---

## Changes Made

### Dockerfile
- Removed: `xpra`, `libjs-jquery`, all jQuery copy/symlink hacks
- Added: `x11vnc`, `novnc`, `websockify` to apt-get install
- Playwright Chromium + Camoufox Firefox retained

### scripts/run.sh
- Replaced xpra startup with: Xvfb → x11vnc → websockify
- DISPLAY_URL points to vnc_lite.html with path=pirate%2F
- Port 6081 exposed and whitelisted in NordVPN

### docker-compose.yml
- Updated DISPLAY_URL to vnc_lite.html variant

### SKILL.md (pirate-dock + browser-display)
- Replaced all xpra references with x11vnc/noVNC stack
- Added Red Herring Graveyard
- Documented correct URL format and pitfalls

### Cleanup
- Deleted: scripts/entrypoint.sh (stale)
- Updated: README.md

---

## Build & Test Log

| Time | Action | Result |
|------|--------|--------|
| 09:00 | Plan created | — |
| 09:02–10:30 | Multiple xpra fix attempts | ALL FAILED (see Red Herring Graveyard) |
| 10:30 | Manual x11vnc+websockify test in running container | ✓ Working locally + through Funnel |
| 10:35 | Dockerfile, run.sh, docker-compose.yml rewritten | ✓ |
| 10:36 | `bash scripts/build.sh --no-cache` | ✓ Build + container start |
| 14:54 | Chromium launched on :1 → Wikipedia | ✓ Visible via VNC |
| 14:55 | End-to-end verification | ✓ PASSED |

### Final Verification
- `curl http://localhost:9876/status` → connected: true, country: South Africa ✓
- Jackett running on :9118 ✓
- noVNC display reachable at Funnel URL ✓
- Chromium visible on remote desktop (Wikipedia loaded) ✓
- NordVPN killswitch ON, Docker subnet whitelisted ✓

---

## Lessons Learned

1. **xpra 3.1 is dead on Ubuntu 22.04.** Do not attempt to resurrect it.
2. **vnc_lite.html is mandatory** for Tailscale Funnel path-based routing. Standard vnc.html silently fails.
3. **Self-contained Docker images** — no symlinks, no host dependencies. Everything copied or installed inside.
4. **Playwright Chromium** works perfectly as the browser inside the container — just needs `--no-sandbox`.
5. **NordVPN whitelist** must include port 6081 and the Docker bridge subnet for the display to be reachable.

---

## Next Steps

- [ ] Anna's Archive Playwright script for automated book downloading
- [ ] Jackett configuration and indexer setup
- [ ] Aria2 integration for download management
