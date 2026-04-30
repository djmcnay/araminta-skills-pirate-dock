# Pirate Dock v3.1 — Display Fix Rebuild

**Started:** 2026-04-30 ~09:00
**Session ref:** CLI, deepseek-v4-pro:cloud
**Goal:** Rebuild container with self-contained xpra display stack. Fix the jQuery path mismatch causing blue screen in xpra HTML5 client. No symlinks, no host dependencies.

---

## Diagnosis (2026-04-30, pre-rebuild)

**Current container state:**
- Container UP 10 hours, NordVPN connected (Johannesburg, NordLynx)
- API on :9876 working
- Jackett: NOT RUNNING

**Display stack — Frankenstein:**
- Xvfb :1 — running ✓
- xpra shadow :1 — ZOMBIE (crashed, `<defunct>`)
- x11vnc on localhost:5901 — running (manually added yesterday, NOT in run.sh/Dockerfile)
- websockify on 0.0.0.0:6081 → localhost:5901 — running (manually added, NOT in run.sh/Dockerfile)
- noVNC served at /pirate/vnc.html — loads but "Failed to connect to server"
- Chromium — running headed on :1, sitting on an AA book page

**Root cause identified:**
xpra's HTML5 client references `js/jquery.js` but the Ubuntu xpra package places jQuery at `js/lib/jquery.js`. This path mismatch causes xpra's HTML5 page to fail silently (blue screen). The fix must be baked into the Dockerfile so it works on any machine without host dependencies.

---

## Plan

- [ ] Dockerfile: add jQuery copy lines after xpra install (self-contained, no symlinks)
- [ ] Remove stale entrypoint.sh (run.sh is canonical)
- [ ] docker compose down
- [ ] bash scripts/build.sh
- [ ] Verify: `curl http://localhost:6081/` returns xpra HTML5 page
- [ ] Verify: `https://araminta.taild3f7b9.ts.net/pirate/` shows browser desktop
- [ ] Verify: API + Jackett operational
- [ ] Update SKILL.md with rebuild outcomes

---

## Execution Log

### 09:00 — Plan created
### 09:02 — Dockerfile patched: jQuery copy lines added
### 09:03 — entrypoint.sh deleted, README updated
### 09:04 — docker compose down (container stopped and removed)
### 09:05 — Running build.sh (rebuild in progress...)
### 09:06 — BUILD FAILED. apt layer cached from previous build; jQuery path `/usr/share/xpra/www/js/lib/jquery.js` doesn't exist.
### 09:06 — Fix: replaced hardcoded cp with `find`-based approach. Kicked off `--no-cache` rebuild.
### 09:07 — Rebuild running (attempt 2, --no-cache)...
### 09:08 — Build succeeded! Container running, xpra HTML5 page loads but jquery.js returns 404.
### 09:33 — Root cause found: jquery.js is a SYMLINK (→ ../../../../javascript/jquery/jquery.js). xpra's built-in HTTP server doesn't follow symlinks. jquery-ui.js is a real file, hence only jquery.js failed.
### 09:34 — Dockerfile updated: `find -type l | readlink -f | cp` to resolve all symlinks in js/lib/ to real files.
### 09:35 — Rebuilding (attempt 3)...
### 09:53 — Build failed: find quoting + missing `file` binary
### 09:54 — Simplified: readlink -f directly, no find
### 09:55 — Build failed: cp through symlink silently fails
### 09:56 — Simplified: rm, then cp from known path
### 09:57 — Build failed: /usr/share/javascript/jquery/jquery.js doesn't exist
### 09:58 — Root cause: libjs-jquery never installed. Added to apt-get.
### 09:59 — BUILD SUCCEEDED. jQuery fixed (200 OK). But xpra shadow returns "server error" on WebSocket.
### 10:18 — xpra start also fails (same error). xpra 3.1 broken.
### 10:30 — Installed x11vnc+novnc+websockify in container. Proven working locally + through Funnel (vnc_lite.html?path=pirate%2F).
### 10:35 — Dockerfile, run.sh, docker-compose.yml all rewritten for x11vnc/noVNC stack.
### 10:36 — FINAL REBUILD running...
