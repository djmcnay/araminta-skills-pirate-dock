#!/usr/bin/env python3
"""List pirate-dock torrent search results, or start one by index.

Run ON THE PI (API is localhost:9876 there):
  python3 start-magnet-download.py --query "UFC 331" --list
  python3 start-magnet-download.py --query "UFC 331" --start 0

Per the skill's standing rules: --list output goes to David for picking;
never --start a torrent he has not chosen.
"""
import argparse
import json
import time
import urllib.parse
import urllib.request

API = "http://localhost:9876"


def get(path: str, timeout: int = 30):
    with urllib.request.urlopen(f"{API}{path}", timeout=timeout) as r:
        return json.load(r)


def post(path: str, body: dict, timeout: int = 20):
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--start", type=int, default=None,
                    help="index from --list output to start downloading")
    args = ap.parse_args()

    results = get(f"/search/torrents?q={urllib.parse.quote(args.query)}").get("results", [])
    if not results:
        print("no results")
        raise SystemExit(1)

    if args.list or args.start is None:
        for i, r in enumerate(results):
            size_gb = (r.get("size") or 0) / 1e9
            print(f"{i:>2} | {r.get('title', '')[:78]:78} | "
                  f"{size_gb:5.1f}GB | S:{r.get('seeders', 0):>4} "
                  f"L:{r.get('peers', 0):>4} | {r.get('source', '?')[:18]}")
        return

    target = results[args.start]
    print("starting:", target.get("title"))
    resp = post("/download/magnet", {"magnet": target["magnet"]})
    print(json.dumps(resp, indent=2))

    time.sleep(5)
    print("--- active downloads ---")
    print(json.dumps(get("/downloads/active"), indent=2))


if __name__ == "__main__":
    main()
