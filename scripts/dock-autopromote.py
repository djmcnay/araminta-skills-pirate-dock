#!/usr/bin/env python3
"""dock-autopromote: DEFAULT promote-on-completion watcher for the pirate-dock.

Policy (David, 24 Sept 2026): unless he has asked for an item NOT to be
promoted, every completed dock download is, on completion:
  1. decode-checked (mid-file frame decode) BEFORE promotion
  2. classified + copied byte-verified into the media library (promoter)
  3. metadata refreshed (Jellyfin scan) and provider-art verified/applied
  4. dock copy deleted ONLY per verified file (never books, never unknowns)

Exceptions: ONLY David's explicit do-not-promote requests (SKIP_PATTERNS).

State machine (per item, journaled in ~/.local/state/dock-autopromote.json):
  None -> decode-checked -> promoted -> done (dock_cleared)
  Failure stages (decode-failed / promote-failed / skipped-no-video) are
  terminal to the AUTOMATIC pipeline: the agent investigates. --force ITEM
  resets a failure and re-runs; --retry-failed retries promote-failed items.
  A changed source signature (size/mtime) resets a failure — a re-downloaded
  item is a new item. State persists after EVERY transition.

Run ON the Pi. Cron: */15 * * * * (flock-serialised).

Usage:
  python3 dock-autopromote.py              # process newly-completed items
  python3 dock-autopromote.py --dry-run    # show decisions, change NOTHING
  python3 dock-autopromote.py --force ITEM # reset failure state + re-run
  python3 dock-autopromote.py --retry-failed  # retry all promote-failed
"""
import fcntl
import json
import re
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DOCK = Path("/home/djmcnay/Documents/GitHub/pirate-dock/downloads")
MEDIA = Path("/home/djmcnay/Media")
SCRIPTS = Path("/home/djmcnay/Documents/GitHub/pirate-dock/scripts")
STATE_DIR = Path("/home/djmcnay/.local/state")
STATE_FILE = STATE_DIR / "dock-autopromote.json"
LOG_FILE = STATE_DIR / "dock-autopromote.log"
JELLYFIN = "http://localhost:8096"
JF_AUTH_FILE = Path("/tmp/jf-auth-header.txt")

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".webm", ".mov", ".wmv", ".ts", ".m4v"}
BOOK_EXTS = {".epub", ".pdf", ".mobi", ".azw3", ".azw"}
# torrent-site droppings — disposable once the item is verified
JUNK_SIDECAR_RE = re.compile(r"(?i)torrent\s+downloaded\s+from|uindex\.org")

# David's explicit exceptions (substring match, case-insensitive).
# Add a line here ONLY when David asks for an item to be left in the dock.
SKIP_PATTERNS = [
    "padella",  # book — reported only, Mac delivery manual
]


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log("WARNING: state file corrupt; starting fresh")
    return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(STATE_FILE)


def is_exempt(name: str) -> bool:
    low = name.lower()
    return any(pat in low for pat in SKIP_PATTERNS)


def has_control_file(item: Path) -> bool:
    """aria2 control file: exact sibling <name>.aria2, or any inside the item
    directory (multi-file packs). Substring matching against the parent's
    other control files false-positives across shared prefixes — never used."""
    if Path(str(item) + ".aria2").exists():
        return True
    if item.is_dir():
        return any(item.rglob("*.aria2"))
    return False


_FFMPEG_CACHE: dict[str, bool] = {}


def _probe_is_video(p: Path) -> bool:
    key = f"{p}:{p.stat().st_size}:{int(p.stat().st_mtime)}"
    if key not in _FFMPEG_CACHE:
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(p)],
                capture_output=True, text=True, timeout=20)
            _FFMPEG_CACHE[key] = bool(r.stdout.strip())
        except Exception:
            _FFMPEG_CACHE[key] = False
    return _FFMPEG_CACHE[key]


def video_files_of(item: Path) -> list[Path]:
    """Full paths throughout — basename re-finding decoded the wrong file when
    subdirectories hold same-named files (audit finding 12)."""
    if item.is_file():
        if item.suffix.lower() in VIDEO_EXTS:
            return [item]
        return [item] if _probe_is_video(item) else []
    return sorted(p for p in item.rglob("*")
                  if p.is_file() and (p.suffix.lower() in VIDEO_EXTS
                                      or (p.suffix == "" and _probe_is_video(p))))


def decode_check(path: Path) -> bool:
    """Mid-file decode requiring evidence of a decoded frame — exit status
    alone accepted zero-frame success (audit finding 12)."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=30)
    try:
        dur = float(r.stdout.strip())
    except ValueError:
        return False
    if dur <= 0:
        return False
    r2 = subprocess.run(
        ["ffmpeg", "-ss", str(dur / 2), "-i", str(path), "-map", "0:v:0",
         "-frames:v", "1", "-f", "null", "-"],
        capture_output=True, text=True, timeout=120)
    return r2.returncode == 0 and "frame=" in (r2.stderr or "")


def source_signature(paths: list[Path]) -> str:
    return "|".join(f"{p.name}:{p.stat().st_size}:{int(p.stat().st_mtime)}"
                    for p in paths)


def library_sizes() -> set[int]:
    sizes = set()
    if MEDIA.exists():
        for p in MEDIA.rglob("*"):
            try:
                if p.is_file():
                    sizes.add(p.stat().st_size)
            except OSError:
                continue
    return sizes


def library_files_by_size() -> dict[int, list[Path]]:
    out: dict[int, list[Path]] = {}
    if MEDIA.exists():
        for p in MEDIA.rglob("*"):
            try:
                if p.is_file():
                    out.setdefault(p.stat().st_size, []).append(p)
            except OSError:
                continue
    return out


def sha256_of(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def film_identity_verified(v: Path, by_size: dict[int, list[Path]]) -> bool:
    """For one-off films, byte-size equality is necessary but not sufficient:
    a DIFFERENT film of coincidentally equal size would pass a size-only
    check and the real source would be deleted un-promoted (audit finding 1,
    cross-invocation). sha256 the library candidate(s) against the source."""
    cands = by_size.get(v.stat().st_size, [])
    if not cands:
        return False
    v_hash = sha256_of(v)
    for c in cands:
        try:
            if sha256_of(c) == v_hash:
                return True
        except OSError:
            continue
    return False


# ── Jellyfin provider-art verification (audit finding 6) ─────────────────────

def jf_auth() -> str | None:
    if JF_AUTH_FILE.exists() and JF_AUTH_FILE.read_text().strip():
        return JF_AUTH_FILE.read_text().strip()
    return None  # watcher never stores credentials; promoter seeds the file


def jf_get_json(path: str, ah: str):
    try:
        req = urllib.request.Request(f"{JELLYFIN}{path}",
                                     headers={"Authorization": ah})
        return json.loads(urllib.request.urlopen(req, timeout=15).read())
    except Exception:
        return None


def verify_provider_art(section: str, title: str) -> str:
    """Ensure the library item has Primary art; apply the first provider image
    if missing. verified|applied|missing|unknown (unknown = cannot check)."""
    ah = jf_auth()
    if not ah:
        return "unknown"
    if section in ("shows", "kids"):
        item_type = "Series"
    elif section in ("movies", "events", "kids-films"):
        item_type = "Movie"
    else:
        return "unknown"
    q = urllib.parse.quote(title)
    data = jf_get_json(
        f"/Items?Recursive=true&IncludeItemTypes={item_type}&SearchTerm={q}"
        f"&Fields=Path&Limit=10", ah)
    if not data:
        return "unknown"
    match = next((i for i in data.get("Items", [])
                  if title.lower() in Path(i.get("Path", "")).name.lower()), None)
    if not match:
        return "missing"
    iid = match["Id"]
    imgs = jf_get_json(f"/Items/{iid}/Images", ah) or []
    if any(i.get("ImageType") == "Primary" for i in imgs):
        return "verified"
    remote = jf_get_json(f"/Items/{iid}/RemoteImages?Type=Primary", ah) or {}
    candidates = remote.get("Images") or []
    if not candidates:
        return "missing"
    first = candidates[0]
    try:
        url = (f"{JELLYFIN}/Items/{iid}/RemoteImages/Download?type=Primary"
               f"&providerName={urllib.parse.quote(first.get('ProviderName', ''))}"
               f"&imageurl={urllib.parse.quote(first.get('Url', ''))}")
        req = urllib.request.Request(url, method="POST",
                                     headers={"Authorization": ah})
        urllib.request.urlopen(req, timeout=30)
        return "applied"
    except Exception:
        return "missing"


# ── deletion: per-file, verified-only (audit findings 2, 5, 13) ──────────────

def _docker_sh(script: str) -> subprocess.CompletedProcess:
    """Run a shell snippet inside the dock's /downloads. The dock container is
    on-demand (usually STOPPED) — fall back to a throwaway bind-mounted sh."""
    r = subprocess.run(
        ["docker", "exec", "pirate-dock", "/bin/sh", "-c", script],
        capture_output=True, text=True, timeout=120)
    if r.returncode == 0:
        return r
    return subprocess.run(
        ["docker", "run", "--rm", "-v",
         "/home/djmcnay/Documents/GitHub/pirate-dock/downloads:/downloads",
         "--entrypoint", "/bin/sh", "pirate-dock-pirate-dock:latest",
         "-c", script],
        capture_output=True, text=True, timeout=180)


def delete_verified_files(item: Path, verified: list[Path],
                          by_size: dict[int, list[Path]]) -> bool:
    """Delete ONLY verified video files, byte-verified sidecars, junk
    sidecars, and the item's own control files. NEVER books, NEVER unknown
    files (audit finding 2). Remove the item dir only when empty. True iff
    the item is fully gone. Paths are built relative to /downloads, where
    the rm runs — nested files need the item-name prefix."""
    def rel(p: Path) -> str:
        return str(p.relative_to(item.parent))

    doomed: list[str] = []
    if item.is_dir():
        keep_books = []
        for p in sorted(item.rglob("*")):
            if not p.is_file():
                continue
            if p in verified:
                doomed.append(rel(p))
            elif p.suffix.lower() in BOOK_EXTS:
                keep_books.append(p.name)
            elif JUNK_SIDECAR_RE.search(p.name):
                doomed.append(rel(p))
            elif p.stat().st_size in by_size:
                doomed.append(rel(p))
            else:
                keep_books.append(p.name)
        for nm in keep_books:
            log(f"  KEEP (book or size not in library): {nm}")
    elif item.suffix.lower() in BOOK_EXTS:
        return False
    else:
        doomed.append(rel(item))
    # the item's own control files are junk once verified
    for ctl in (item.parent / (item.name + ".aria2"),):
        if ctl.exists():
            doomed.append(ctl.name)

    inner = " && ".join(f"rm -f -- {shlex.quote(d)}" for d in doomed) or "true"
    if item.is_dir():
        inner += f" && rmdir -- {shlex.quote(item.name)} 2>/dev/null || true"
    r = _docker_sh(f"cd /downloads && {inner}")
    if r.returncode != 0:
        log(f"  DELETE FAILED {item.name}: {r.stderr.strip()[:200]}")
        return False
    if item.exists():
        log(f"  dock copy partially cleared (unverified files kept): {item.name}")
        return False
    return True


# ── per-item state machine (audit findings 4, 14) ────────────────────────────

def promote_item(item: Path, state: dict, dry: bool) -> None:
    name = item.name
    st = state.setdefault(name, {})
    stage = st.get("stage")

    # source-change invalidation: a re-downloaded item is a new item
    vids = video_files_of(item)
    sig = source_signature(vids) if vids else None
    if stage in ("decode-failed", "promote-failed", "skipped-no-video") \
            and sig and st.get("source_sig") not in (None, sig):
        log(f"source changed since {stage}: {name} — resetting")
        st.clear()
        stage = None

    if stage in ("done", "book-noted", "skipped", "decode-failed",
                 "promote-failed", "skipped-no-video", "superseded-duplicate"):
        return
    if has_control_file(item):
        return  # still downloading or restarted; not our turn

    if item.suffix.lower() in BOOK_EXTS:
        log(f"book item (reported only, never promoted): {name}")
        st.update(stage="book-noted", at=time.time())
        save_state(state)
        return

    if not vids:
        log(f"SKIP {name}: no video files found")
        st.update(stage="skipped-no-video", source_sig=sig, at=time.time())
        save_state(state)
        return

    if dry:
        log(f"DRY: would process {name}")
        return

    # ── 0. pre-promote duplicate check: if the library ALREADY holds an
    # identity-verified copy of every video, skip promotion (a twin folder
    # would be created) and go straight to clearing the duplicate.
    by_size0 = library_files_by_size()
    if all(film_identity_verified(v, by_size0) for v in vids):
        log(f"already in library (identity-verified) — clearing duplicate: {name}")
        if delete_verified_files(item, vids, by_size0):
            st.update(stage="done", dock_cleared=True, at=time.time())
            log(f"cleared duplicate dock copy: {name}")
            save_state(state)
        else:
            st.update(stage="promoted", at=time.time())
            save_state(state)
        return

    # ── 1. decode-check every video BEFORE promotion (skip if already done)
    if stage not in ("promoted", "decode-checked"):
        sig = source_signature(vids)
        for v in vids:
            if not decode_check(v):
                log(f"DECODE-FAIL {name}: {v.name} — flagged, NOT promoted")
                st.update(stage="decode-failed", source_sig=sig, at=time.time())
                save_state(state)
                return
        st.update(stage="decode-checked", source_sig=sig, at=time.time())
        save_state(state)

    # ── 2. promote (byte-verified inside the promoter; it aborts on conflicts)
    r = subprocess.run(
        ["python3", str(SCRIPTS / "promote-to-media.py"), name],
        capture_output=True, text=True, timeout=7200)
    out = (r.stdout + r.stderr).strip()
    log(f"promote {name}: rc={r.returncode}")
    if out:
        for line in out.splitlines()[-6:]:
            log(f"  | {line}")
    if r.returncode != 0:
        st.update(stage="promote-failed", at=time.time())
        save_state(state)
        return
    already = "already promoted" in out
    st.update(stage="promoted", at=time.time(), already_promoted=already)
    save_state(state)

    # ── 3. provider-art verification on the LIBRARY copy (audit finding 6).
    # Never blocks deletion; failures are flagged for agent-side repair
    # per the jellyfin-thumbnails skill (RemoteImages/Download first).
    try:
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location(
            "_promo", str(SCRIPTS / "promote-to-media.py"))
        if spec is None or spec.loader is None:
            raise ImportError("cannot load promoter module")
        promo = _ilu.module_from_spec(spec)
        spec.loader.exec_module(promo)
        section, title, _year = promo.classify(item)
        if not st.get("art"):
            st["art"] = verify_provider_art(section, title)
            log(f"  provider art: {st['art']} — {name}")
            save_state(state)
    except Exception as e:
        log(f"  art check error ({e}) — flagged for agent review")
        st["art"] = "unknown"
        save_state(state)

    # ── 4. verify + clear: identity-verified deletion (audit findings 1, 2, 5).
    # Byte-size equality alone authorized deleting a DIFFERENT film of
    # coincidentally equal size. So: sha256 every video file against the
    # library candidates of equal size; only identity-matched files are
    # deletable. Episode packs (multi-file) get size-set membership.
    vids = video_files_of(item)
    by_size = library_files_by_size()
    verified: list[Path] = []
    for v in vids:
        if len(vids) == 1:
            ok = film_identity_verified(v, by_size)   # single-file: sha256
        else:
            ok = v.stat().st_size in by_size          # pack: size-set member
        if ok:
            verified.append(v)
        else:
            log(f"  IDENTITY-UNVERIFIED (kept): {v.name}")
    if verified and len(verified) == len(vids):
        if delete_verified_files(item, verified, by_size):
            st.update(stage="done", dock_cleared=True, at=time.time())
            log(f"cleared dock copy (identity-verified): {name}")
        else:
            st.update(stage="promoted", at=time.time())  # resumable next pass
    else:
        missing = [v.name for v in vids if v not in verified]
        if st.get("already_promoted"):
            # library already holds this content from a DIFFERENT release
            # (different encode/bytes): superseded duplicate — terminal to the
            # automatic pipeline, agent reviews and deletes per David's
            # standing policy (never auto-delete on name/size mismatch alone)
            st.update(stage="superseded-duplicate", at=time.time(),
                      kept_reason="identity differs from library copy")
            log(f"SUPERSEDED (library holds different encode): {name}")
        else:
            log(f"KEEP dock copy (identity not established): {missing}")
            st.update(stage="promoted", at=time.time())
    save_state(state)


def main() -> None:
    args = sys.argv[1:]
    dry = "--dry-run" in args
    force_item = None
    if "--force" in args:
        i = args.index("--force")
        force_item = args[i + 1] if i + 1 < len(args) else None

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = (STATE_DIR / "dock-autopromote.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another instance is running; exiting")
        return

    if dry:
        log("DRY RUN — evaluating decisions, changing nothing, persisting nothing")
        for entry in sorted(DOCK.iterdir()):
            if entry.name.startswith(".") or entry.name.endswith(".aria2"):
                continue  # job logs, temp files, aria2 control files
            if has_control_file(entry):
                log(f"DRY: {entry.name} — in flight (control file)")
            elif is_exempt(entry.name):
                log(f"DRY: {entry.name} — exempt (SKIP list)")
            elif not video_files_of(entry):
                log(f"DRY: {entry.name} — no video files, would flag skipped-no-video")
            else:
                log(f"DRY: {entry.name} — complete, would promote")
        return

    state = load_state()

    if force_item:
        # audit finding 13: only DIRECT children of the dock, no traversal
        if "/" in force_item or force_item.startswith("."):
            log(f"refusing ambiguous --force target: {force_item}")
            sys.exit(2)
        target = DOCK / force_item
        if not target.parent.samefile(DOCK) or not target.exists():
            log(f"force target not a dock child or not found: {force_item}")
            sys.exit(2)
        st = state.setdefault(force_item, {})
        if st.get("stage") in ("decode-failed", "promote-failed",
                               "skipped-no-video", "done",
                               "superseded-duplicate"):
            log(f"resetting state for {force_item} (was {st.get('stage')})")
            st.clear()
        promote_item(target, state, dry=False)
        save_state(state)
        return

    if "--retry-failed" in args:
        for nm, st in list(state.items()):
            if st.get("stage") == "promote-failed":
                target = DOCK / nm
                if target.exists():
                    log(f"retrying promote-failed: {nm}")
                    st.clear()
                    promote_item(target, state, dry=False)
        save_state(state)
        return

    if not DOCK.exists():
        log(f"dock downloads dir missing: {DOCK}")
        return
    for entry in sorted(DOCK.iterdir()):
        if entry.name.startswith(".") or entry.name.endswith(".aria2"):
            continue  # job logs, temp files, aria2 control files
        if is_exempt(entry.name):
            st = state.setdefault(entry.name, {})
            if st.get("stage") != "skipped":
                log(f"exempt (SKIP list): {entry.name}")
                st.update(stage="skipped", at=time.time())
                save_state(state)
            continue
        try:
            promote_item(entry, state, dry=False)
        except Exception as e:
            log(f"ERROR processing {entry.name}: {e}")


if __name__ == "__main__":
    main()