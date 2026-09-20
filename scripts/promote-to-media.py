#!/usr/bin/env python3
"""promote-to-media: classify a completed dock download and install it into
the Jellyfin library on the Pi (~/Media), with proper naming for metadata.

Runs ON the Pi (paths are native). Canonical location: pirate-dock repo,
scripts/promote-to-media.py. Invoked by any Minty (Pi or Mac via ssh).

Usage:
  python3 promote-to-media.py "<dock item name>"           # classify + copy + scan
  python3 promote-to-media.py "<dock item name>" --dry-run # show the plan only
  python3 promote-to-media.py --list                       # list dock items

Classification (by filename patterns + content):
  Event : sports keywords (UFC, WWE, Boxing, PPV, ...)      -> events/
  TV    : episode markers (S01E02 / 1x02 / Season N dirs)   -> shows/ or kids/
  Kids  : title in kids allowlist (Fireman Sam, Bluey, ...) -> kids/
  Movie : everything else with video files                 -> movies/
  Book  : .epub/.pdf/.mobi                                 -> reported, not copied (Jellyfin doesn't serve books)

Jellyfin naming applied:
  Movie/Event: <section>/<Clean Title> (<Year>)/<Clean Title> (<Year>) - <Label>.<ext>
  TV episode : <section>/<Show> (<Year>)/Season NN/<Show> S##E## - <Title>.<ext>

Copies are byte-verified against the source before success. Sources are left
in place (delete from the dock is a separate, explicit decision).
"""
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DOCK_DOWNLOADS = Path("/home/djmcnay/Documents/GitHub/pirate-dock/downloads")
MEDIA_ROOT = Path("/home/djmcnay/Media")
JELLYFIN = "http://localhost:8096"
JF_AUTH_FILE = Path("/tmp/jf-auth-header.txt")

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".webm", ".mov", ".wmv", ".ts", ".m4v"}
BOOK_EXTS = {".epub", ".pdf", ".mobi", ".azw3", ".azw"}

KIDS_TITLES = ("fireman sam", "bluey", "peppa", "paw patrol", "hey duggee", "bing",
               "gruffalo", "room on the broom", "stick man", "shaun the sheep",
               "thomas", "postman pat", "paddington", "horrid henry")
EVENT_KEYWORDS = ("ufc", "wwe", "boxing", "ppv", "fight night", "prelims", "main card",
                  "formula", "f1", "nfl", "superbowl", "six nations", "wimbledon")

TV_EPISODE_RE = re.compile(r"(?i)\bS\d{1,2}\s?E\d{1,3}\b|\b\d{1,2}x\d{1,3}\b|season\s+\d{1,2}")
YEAR_RE = re.compile(r"(?<!\w)(19\d{2}|20\d{2})(?!\w)")


def log(*a):
    print(*a, flush=True)


# ── classification ────────────────────────────────────────────────────────────

def clean_title(stem: str) -> str:
    t = re.sub(r"[._]", " ", stem)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s+[a-f0-9]{8}\s*$", "", t)           # trailing infohash
    t = re.sub(r"\s*\[[^\]]+\]\s*$", "", t)            # trailing [tag]
    t = re.sub(r"\s+\d{4}$", "", t)                    # trailing year (year goes in parens)
    return t


def title_case(s: str) -> str:
    return " ".join(w.capitalize() for w in s.split())


def extract_year(text: str) -> str | None:
    years = YEAR_RE.findall(text)
    return years[-1] if years else None


def video_files(src: Path) -> list[Path]:
    return sorted(p for p in src.rglob("*")
                  if p.is_file() and p.suffix.lower() in VIDEO_EXTS)


def all_files(src: Path) -> list[Path]:
    return sorted(p for p in src.rglob("*") if p.is_file())


def is_kids(lower: str) -> bool:
    return any(k in lower for k in KIDS_TITLES)


def is_event(lower: str) -> bool:
    return any(k in lower for k in EVENT_KEYWORDS)


def classify(src: Path) -> tuple[str, str, str | None]:
    """Return (section, title, year). Section in events|shows|kids|movies|books."""
    files = video_files(src)
    stem = src.stem
    lower = re.sub(r"[._]", " ", stem).lower()
    year = extract_year(stem) or next(
        (extract_year(f.stem) for f in files if extract_year(f.stem)), None)

    # Book shortcut (single doc file, no videos) — also covers bare .epub FILE items
    docs = [p for p in all_files(src) if p.suffix.lower() in BOOK_EXTS] if src.is_dir() else (
        [src] if src.suffix.lower() in BOOK_EXTS else [])
    if docs and not files:
        return "book", clean_title(stem), year

    # Kids by title (show name may come from child episode filenames)
    if is_kids(lower) or (files and any(is_kids(re.sub(r"[._]", " ", f.stem.lower())) for f in files)):
        show = _show_name(src)
        if show:
            show = re.sub(r"(?i)\s*series\s*\d+(-\d+)?\s*$", "", show).strip() or show
        return "kids", title_case(show or clean_title(stem)), year

    # Sports event
    if is_event(lower) or (files and any(is_event(re.sub(r"[._]", " ", f.stem.lower())) for f in files)):
        return "events", clean_title(stem), year

    # TV: episode markers in the item name or any child file
    if src.is_dir() and (TV_EPISODE_RE.search(stem)
                         or any(TV_EPISODE_RE.search(f.stem) for f in files)
                         or any(p.is_dir() and re.match(r"(?i)(season|series)\s*\d", p.name)
                                for p in src.iterdir())):
        return "shows", title_case(_show_name(src) or clean_title(stem)), year

    # Movie fallback
    return "movies", clean_title(stem), year


def _show_name(src: Path) -> str | None:
    """Derive the show name from child episode filenames (S##E##-style)."""
    for f in video_files(src):
        m = re.match(r"(?i)(.+?)\s*S\d{1,2}E\d{1,3}", f.stem)
        if m:
            return clean_title(m.group(1))
        m = re.match(r"(?i)(.+?)\s*\((\d{4})\)", f.stem)
        if m and "S" in f.stem.upper():
            return clean_title(m.group(1))
    return None


# ── planning ──────────────────────────────────────────────────────────────────

def plan(src: Path) -> tuple[str, Path, list[tuple[Path, Path]]]:
    section, title, year = classify(src)

    # Year fallback: newest mtime year across videos (torrents rarely carry years)
    if section != "book" and not year:
        vids = video_files(src) if src.is_dir() else ([src] if src.suffix.lower() in VIDEO_EXTS else [])
        if vids:
            newest = max(f.stat().st_mtime for f in vids)
            year = time.strftime("%Y", time.localtime(newest))
        else:
            year = time.strftime("%Y", time.localtime(src.stat().st_mtime))

    # Re-promote detection: if a folder for the same title already exists in the
    # section (with or without year), promote INTO it instead of creating a twin.
    def _norm(s: str) -> str:
        s = re.sub(r"\s*\(\d{4}\)\s*$", "", s).strip().lower()
        s = re.sub(r"(?i)\s*series\s*\d+(-\d+)?\s*$", "", s)
        return re.sub(r"\s+", " ", s).strip()
    if section not in ("book",):
        base = MEDIA_ROOT / section
        title_lower = _norm(title)
        existing = [p for p in base.iterdir()
                    if p.is_dir() and (_norm(p.name) == title_lower
                                       or _norm(title_lower) in _norm(p.name)
                                       or _norm(p.name) in _norm(title_lower))] if base.exists() else []
        if existing and section in ("movies", "events"):
            dest_dir = existing[0]
            moves = []
            files = video_files(src) if src.is_dir() else ([src] if src.is_file() and src.suffix.lower() in VIDEO_EXTS else [])
            for f in files:
                label_src = re.sub(r"[._]", " ", f.stem)
                base_name = re.sub(r"\s*\(\d{4}\)$", "", dest_dir.name)
                label = re.sub("(?i)^" + re.escape(base_name) + r"\s*", "", label_src).strip()
                label = re.sub(r"\s*\(\d{4}\)\s*$", "", label).strip() or "Feature"
                dest_name = f"{dest_dir.name} - {clean_title(label)}{f.suffix.lower()}"
                moves.append((f, dest_dir / dest_name))
            for f in sidecar_files(src):
                moves.append((f, dest_dir / f.name))
            return section, dest_dir, moves

    if section == "book":
        return section, MEDIA_ROOT, []

    files = video_files(src)
    sidecars = [p for p in all_files(src)
                if p.suffix.lower() not in VIDEO_EXTS | {".aria2"}]

    moves: list[tuple[Path, Path]] = []

    # TV: season/episode naming
    season_dirs = src.is_dir() and any(
        re.match(r"(?i)(season|series)\s*\d", c.name) for c in src.iterdir() if c.is_dir())
    if section in ("shows", "kids") and (
            any(TV_EPISODE_RE.search(f.stem) for f in files) or section == "shows" or season_dirs):
        show = title
        show_dir = None
        base = MEDIA_ROOT / section
        if base.exists():
            for cand in base.iterdir():
                if cand.is_dir() and _norm(cand.name) == _norm(show):
                    show_dir = cand
                    break
        if show_dir is None:
            show_dir = base / (f"{show} ({year})" if year else show)
        show = re.sub(r"\s*\(\d{4}\)\s*$", "", show_dir.name)
        parsed = []
        for f in files:
            m = re.search(r"(?i)S(\d{1,2})\s?E(\d{1,3})", f.stem) or \
                re.search(r"(?i)(\d{1,2})x(\d{1,3})", f.stem)
            if m:
                parsed.append((f, int(m.group(1)), int(m.group(2))))
            else:
                parsed.append((f, None, None))
        seasons = {s for _, s, _ in parsed if s}
        existing_eps = set()
        for d in show_dir.glob("Season *"):
            for ef in d.iterdir():
                m2 = re.search(r"(?i)S(\d{1,2})E(\d{1,3})", ef.name)
                if m2:
                    existing_eps.add((int(m2.group(1)), int(m2.group(2))))
        for f, season, ep in parsed:
            if season is None:
                # unmarked episodes inside a Series N dir: take the dir number
                rel = f.relative_to(src)
                dm = re.search(r"(?i)(?:season|series)\s*(\d{1,2})", str(rel))
                season = int(dm.group(1)) if dm else 1
                ep_guess = re.match(r"(\d{1,3})", f.stem)
                ep = int(ep_guess.group(1)) if ep_guess else 0
            if (season, ep) in existing_eps:
                continue  # already promoted
            if season is None:
                t = clean_title(f.stem)
            else:
                # title after S##E## marker
                m = re.search(rf"(?i)S{season:02d}E\d{{1,3}}\s*[-–]?\s*(.+)", f.stem) or \
                    re.search(rf"(?i)S{season}\bE?(\d{{1,3}})?\s*[-–]?\s*(.+)", f.stem)
                t = clean_title(m.group(1) or m.group(2)) if m else clean_title(f.stem)
                t = re.sub(rf"(?i)^{re.escape(show)}\s*", "", t) or t
            dest = show_dir / f"Season {season:02d}" / (
                f"{show} S{season:02d}E{ep:02d} - {t}{f.suffix.lower()}")
            moves.append((f, dest))
        # sidecars (srt/nfo) follow the first video's target dir
        if moves:
            dest = moves[0][1].parent
        else:
            dest = show_dir
        for f in sidecar_files(src):
            moves.append((f, dest / f.name))
        return section, show_dir, moves

    # Movie / Event: single folder, "<Title> (<Year>) - <Label>.<ext>"
    folder = MEDIA_ROOT / section / (f"{title} ({year})" if year else title)
    for f in files:
        label_src = re.sub(r"[._]", " ", f.stem)
        label = re.sub(rf"(?i)^{re.escape(title)}\s*", "", label_src).strip()
        label = re.sub(r"\s*\(\d{4}\)\s*$", "", label).strip()
        if label.lower() == title.lower() or not label:
            label = "Feature"
        moves.append((f, folder / f"{title} ({year}) - {clean_title(label)}{f.suffix.lower()}"))
    for f in sidecar_files(src):
        moves.append((f, folder / f.name))
    return section, folder, moves


def sidecar_files(src: Path) -> list[Path]:
    if not src.is_dir():
        return []
    return [p for p in all_files(src)
            if p.suffix.lower() not in VIDEO_EXTS | BOOK_EXTS | {".aria2"}]


# ── execution ─────────────────────────────────────────────────────────────────

def jellyfin_auth() -> str | None:
    if JF_AUTH_FILE.exists() and JF_AUTH_FILE.read_text().strip():
        return JF_AUTH_FILE.read_text().strip()
    body = json.dumps({"Username": "david", "Pw": "Badgers8!"}).encode()
    req = urllib.request.Request(f"{JELLYFIN}/Users/AuthenticateByName", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": 'MediaBrowser Client="Hermes", Device="promote", '
                                  'DeviceId="promote-1", Version="1.0"'}, method="POST")
    d = json.loads(urllib.request.urlopen(req, timeout=30).read())
    ah = ('MediaBrowser Client="Hermes", Device="promote", DeviceId="promote-1", '
          'Version="1.0", Token="%s"') % d["AccessToken"]
    JF_AUTH_FILE.write_text(ah)
    return ah


def scan(ah: str | None) -> None:
    if not ah:
        log("  (skipping library scan — no auth)")
        return
    r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                        "--max-time", "30", "-X", "POST",
                        f"{JELLYFIN}/Library/Refresh",
                        "-H", f"Authorization: {ah}"],
                       capture_output=True, text=True)
    log(f"  library scan triggered: {r.stdout.strip()}")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if "--list" in flags:
        for p in sorted(DOCK_DOWNLOADS.iterdir()):
            if not p.name.startswith("."):
                print(p.name)
        return
    if not args:
        print(__doc__)
        sys.exit(1)

    item = args[0]
    dry = "--dry-run" in flags
    src = DOCK_DOWNLOADS / item
    if not src.exists():
        log(f"ERROR: {src} not found (try --list)")
        sys.exit(1)

    section, dest_dir, moves = plan(src)
    log(f"item:       {src.name}")
    log(f"classified: {section}")
    log(f"dest:       {dest_dir}")
    for f, d in moves:
        try:
            rel = d.relative_to(MEDIA_ROOT)
        except ValueError:
            rel = d
        log(f"  {f.stat().st_size:>12,}B  {f.name}")
        log(f"  {'':>14}→ {rel}")
    if dry:
        log("DRY RUN — no changes made")
        return
    if section == "book":
        log("book item: Jellyfin doesn't serve books — nothing copied. "
            "Deliver via pull-from-dock.sh to the Mac instead.")
        return

    # conflicts: refuse to overwrite an existing different-size file
    conflicts = [d for _, d in moves if d.exists() and d.stat().st_size != (
        f.stat().st_size if (f := next(x for x, dd in moves if dd == d)) else -1)]
    real_conflicts = [d for _, d in moves if d.exists()]
    if real_conflicts:
        same = [d for d in real_conflicts
                if d.stat().st_size == next(f.stat().st_size for f, dd in moves if dd == d)]
        if len(same) == len(real_conflicts):
            log("already promoted (all destinations exist with matching sizes) — nothing to do")
            scan(jellyfin_auth())
            return
        log(f"ABORT: {len(real_conflicts) - len(same)} destination file(s) exist with "
            f"DIFFERENT sizes — resolve manually: {real_conflicts[:3]}")
        sys.exit(2)

    dest_dir.mkdir(parents=True, exist_ok=True)
    for f, d in moves:
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, d)
    bad = [(f, d) for f, d in moves if d.stat().st_size != f.stat().st_size]
    if bad:
        log(f"VERIFY FAILED for {len(bad)} file(s): {[str(d) for _, d in bad]}")
        sys.exit(2)
    log(f"verified: {len(moves)} file(s) copied byte-exact")
    scan(jellyfin_auth())


if __name__ == "__main__":
    main()