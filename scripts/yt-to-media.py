#!/usr/bin/env python3
"""yt-to-media: download YouTube videos as MP4 into the Jellyfin library.

Canonical location: pirate-dock repo, scripts/yt-to-media.py (runs on the Pi).

Usage:
  python3 yt-to-media.py <URL> [--show "Show Name"] [--year 2020] [--kids]
  python3 yt-to-media.py <URL> [--movie]                # single movie, no show structure
  python3 yt-to-media.py <URL> --list-formats           # inspect formats only

Behaviour:
  - Downloads best MP4-compatible stream capped at 1080p (bv*[height<=1080]+ba,
    merged to mp4 via ffmpeg — Pi direct-plays H.264 mp4 everywhere).
  - TV mode (default): lands in /home/djmcnay/Media/kids (or shows if --no-kids)
      <Show> (<Year>)/Season NN/<Show> S##E## - <Title>.mp4
    Episode number from --episode N, or auto-increment from existing files.
  - Movie mode (--movie): <Title> (<Year>)/<Title> (<Year>).mp4 in movies/ or kids/.
  - Title/year auto-pulled from yt-dlp metadata if not supplied.
  - Fires a Jellyfin library scan at the end (auth via /tmp/jf-auth-header.txt
    recipe from the jellyfin-media-server skill).
  - Requires: ~/.local/bin/yt-dlp, ffmpeg. Run ON the Pi.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

YT = "/home/djmcnay/.local/bin/yt-dlp"
MEDIA = Path("/home/djmcnay/Media")
JELLYFIN = "http://localhost:8096"
JF_AUTH = Path("/tmp/jf-auth-header.txt")


def log(*a):
    print(*a, flush=True)


def yt_meta(url: str) -> dict:
    r = subprocess.run([YT, "--dump-json", "--no-playlist", url],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        log(f"ERROR fetching metadata: {r.stderr[-400:]}")
        sys.exit(1)
    return json.loads(r.stdout)


def sanitize(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:120]


def jellyfin_scan():
    ah = JF_AUTH.read_text().strip() if JF_AUTH.exists() else None
    if not ah:
        body = json.dumps({"Username": "david", "Pw": "Badgers8!"}).encode()
        req = urllib.request.Request(f"{JELLYFIN}/Users/AuthenticateByName", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": 'MediaBrowser Client="Hermes", Device="ytdl", '
                                      'DeviceId="yt-1", Version="1.0"'}, method="POST")
        import urllib.request
        d = json.loads(urllib.request.urlopen(req, timeout=30).read())
        ah = ('MediaBrowser Client="Hermes", Device="ytdl", DeviceId="yt-1", '
              'Version="1.0", Token="%s"') % d["AccessToken"]
        JF_AUTH.write_text(ah)
    import urllib.request
    req = urllib.request.Request(f"{JELLYFIN}/Library/Refresh", method="POST",
        headers={"Authorization": ah})
    code = urllib.request.urlopen(req, timeout=30).status
    log(f"library scan: {code}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--show", help="Show name (TV mode)")
    ap.add_argument("--year", type=str, help="Release year")
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument("--episode", type=int, default=None)
    ap.add_argument("--kids", action="store_true", default=True)
    ap.add_argument("--no-kids", dest="kids", action="store_false")
    ap.add_argument("--movie", action="store_true")
    ap.add_argument("--list-formats", action="store_true")
    args = ap.parse_args()

    meta = yt_meta(args.url)
    title = sanitize(args.show or meta.get("title", "video"))
    year = args.year or (meta.get("upload_date") or "")[:4]
    channel = meta.get("channel") or meta.get("uploader") or ""
    duration = meta.get("duration") or 0
    log(f"title:   {meta.get('title')}")
    log(f"channel: {channel} | duration: {duration // 60}m{duration % 60:02d}s")

    if args.list_formats:
        subprocess.run([YT, "-F", "--no-playlist", args.url])
        return

    section = "kids" if args.kids else "shows"
    if args.movie:
        section = "kids" if args.kids else "movies"

    # destination
    if args.movie:
        folder = MEDIA / section / f"{title} ({year})" if year else MEDIA / section / title
        dest = folder / f"{title} ({year}).mp4" if year else folder / f"{title}.mp4"
    else:
        show = args.show or channel or title
        show = sanitize(show)
        show_dir = MEDIA / section / f"{show} ({year})" if year else MEDIA / section / show
        season = args.season or 1
        if args.episode:
            ep = args.episode
        else:
            season_dir = show_dir / f"Season {season:02d}"
            existing = []
            if season_dir.exists():
                for f in season_dir.glob("*.mp4"):
                    m = re.search(rf"(?i)S{season:02d}E(\d{{1,3}})", f.name)
                    if m:
                        existing.append(int(m.group(1)))
            ep = max(existing) + 1 if existing else 1
        ep_title = meta.get("title") or f"Episode {ep:02d}"
        dest = show_dir / f"Season {season:02d}" / f"{show} S{season:02d}E{ep:02d} - {sanitize(ep_title)}.mp4"
        folder = show_dir

    log(f"dest:    {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        log(f"ERROR: destination already exists: {dest}")
        sys.exit(2)

    # download
    tmp_pattern = str(dest.parent / (".yt-dl-" + str(int(time.time())) + ".%(ext)s"))
    r = subprocess.run(
        [YT,
         "-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
         "--merge-output-format", "mp4",
         "--no-playlist",
         "-o", tmp_pattern,
         args.url],
        capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        log(f"ERROR download failed: {r.stderr[-500:]}")
        sys.exit(1)
    tmp_file = None
    for cand in dest.parent.glob(".yt-dl-*.mp4"):
        tmp_file = cand
        break
    if not tmp_file:
        log("ERROR: downloaded file not found")
        sys.exit(1)
    tmp_file.rename(dest)
    log(f"downloaded: {dest.stat().st_size:,} bytes")

    # cleanup tmp files (non-mp4 leftovers)
    for leftover in dest.parent.glob(".yt-dl-*"):
        leftover.unlink(missing_ok=True)

    log("scanning Jellyfin library...")
    jellyfin_scan()
    log(f"DONE: {dest}")


if __name__ == "__main__":
    main()