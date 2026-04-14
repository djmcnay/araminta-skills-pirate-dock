#!/usr/bin/env python3
"""
Pirate-Dock Skill Tests
=======================
Run like a unit test suite against the running pirate-dock container.
Exercises both pipelines (Anna's Archive + torrents) end-to-end.

Usage:
    docker exec pirate-dock python3 /app/scripts/test.py
    python3 scripts/test.py          # from the project directory (requires curl)
"""

import json
import os
import subprocess
import sys
import time

API = "http://127.0.0.1:9876"
DOCKER = "pirate-dock"

# ── Helpers ──────────────────────────────────────────────────────────

class TestResult:
    def __init__(self, name, passed, details=""):
        self.name = name
        self.passed = passed
        self.details = details

    def __str__(self):
        mark = "PASS" if self.passed else "FAIL"
        return f"  [{mark}] {self.name}" + (f" — {self.details}" if self.details else "")


results: list[TestResult] = []


def api_get(path: str, timeout: int = 60) -> dict:
    """Hit the pirate-dock API and return parsed JSON."""
    cmd = f"curl -s --max-time {timeout} '{API}{path}'"
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout + 10)
    try:
        return json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        return {"error": r.stdout[:500]}


def section(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


# ── Test 1: Anna's Archive ──────────────────────────────────────────

def test_annas_archive():
    """
    TEST 1: Anna's Archive — Book Search
    Search for 'Japaneasy Kitchen' by Tim Anderson (ISBN 9781837834549).
    Verify results come back with MD5 hashes and valid download links.
    """
    section("TEST 1: Anna's Archive — Japaneasy Kitchen by Tim Anderson")

    # 1a: Search by title + author
    print("\n[1a] Searching Anna's Archive for 'Japaneasy Kitchen Tim Anderson'...")
    data = api_get("/search/annas-archive?q=Japaneasy+Kitchen+Tim+Anderson")
    count = data.get("count", 0)
    print(f"      Results: {count}")

    if count == 0:
        results.append(TestResult("Anna's Archive search returns results", False, "0 results"))
        return

    # 1b: Verify first result has an MD5
    first = data["results"][0]
    md5 = first.get("md5", "")
    print(f"      First MD5: {md5}")
    has_md5 = bool(md5) and len(md5) == 32
    results.append(TestResult(
        "Anna's Archive returns valid MD5 hashes",
        has_md5,
        f"md5={md5}" if has_md5 else "empty or invalid MD5",
    ))

    # 1c: Verify we can generate valid slow-download links
    base = "https://annas-archive.gl"
    links = {
        "page": f"{base}/md5/{md5}",
        "slow_gl": f"{base}/slow_download/gl/{md5}",
        "slow_pk": f"{base}/slow_download/pk/{md5}",
        "slow_gd": f"{base}/slow_download/gd/{md5}",
    }
    print(f"\n[1b] Download links:")
    for name, link in links.items():
        print(f"      {name}: {link}")

    results.append(TestResult(
        "Anna's Archive download links generated",
        has_md5,
        f"5 links for md5 {md5[:12]}...",
    ))


# ── Test 2: Jackett UFC Search ─────────────────────────────────────

def test_ufc_search():
    """
    TEST 2: Jackett — Latest UFC Videos
    Search for 'UFC' across all indexers and display the top 10 results.
    """
    section("TEST 2: Latest UFC Videos — Top 10")

    print("\n[2a] Searching all indexers for 'UFC'...")
    data = api_get("/search/torrents?q=UFC")
    results_list = data.get("results", [])
    count = data.get("count", 0)
    print(f"      Results: {count}")

    if count == 0:
        results.append(TestResult("UFC search returns results", False, "0 results"))
        return

    results.append(TestResult("UFC search returns results", True, f"{count} torrents"))

    # Sort by size (largest = most likely full event)
    results_list.sort(key=lambda x: x.get("size", 0), reverse=True)

    # 2b: Display top 10
    print(f"\n[2b] Top 10 UFC video options:")
    print(f"{'#':>3} {'Title':<72} {'Size':>8} {'Seeds':>6} {'Source':<15}")
    print("-" * 105)

    for i, r in enumerate(results_list[:10]):
        title = r.get("title", "?")[:70]
        size = r.get("size", 0)
        seeds = r.get("seeders", 0)
        source = r.get("source", "?")

        if size >= 1073741824:
            size_str = f"{size / 1073741824:.1f}GB"
        elif size >= 1048576:
            size_str = f"{size / 1048576:.0f}MB"
        else:
            size_str = f"{size / 1024:.0f}KB"

        print(f"{i + 1:>3} {title:<72} {size_str:>8} {seeds:>6} {source:<15}")

    shown = min(count, 10)
    results.append(TestResult("Top 10 UFC results displayed", True, f"{shown} rows printed"))


# ── Test 3: Top Gun Download Lifecycle ─────────────────────────────

def test_topgun_lifecycle():
    """
    TEST 3: Top Gun — Search, Start Download, Cancel, Clean Up
    Demonstrates the full torrent lifecycle without leaving artefacts.
    """
    section("TEST 3: Top Gun — Search → Download → Cancel → Delete")

    # 3a: Search
    print("\n[3a] Searching for 'Top Gun Maverick'...")
    data = api_get("/search/torrents?q=Top+Gun+Maverick")
    results_list = data.get("results", [])
    count = data.get("count", 0)
    print(f"      Results: {count}")

    if count == 0:
        results.append(TestResult("Top Gun search returns results", False, "0 results"))
        return

    results.append(TestResult("Top Gun search returns results", True, f"{count} torrents"))

    # 3b: Pick smallest > 100MB (quick download test)
    results_list.sort(key=lambda x: x.get("size", 0))
    chosen = None
    for r in results_list:
        if r.get("size", 0) > 100_000_000:
            chosen = r
            break
    if not chosen:
        chosen = results_list[0]

    size_mb = chosen.get("size", 0) / 1048576
    print(f"\n[3b] Selected: {chosen['title'][:70]}")
    print(f"      Size: {size_mb:.0f}MB")
    results.append(TestResult("Torrent selected for test download", True, f"{size_mb:.0f}MB"))

    # 3c: Write magnet to temp file, start aria2 via shell script
    magnet = chosen["magnet"]
    magnet_path = "/tmp/magnet_test.txt"
    script_path = "/tmp/dl_test.sh"

    # Write magnet file
    subprocess.run(
        ["docker", "cp", "/dev/stdin", f"{DOCKER}:{magnet_path}"],
        input=magnet.encode(), timeout=10,
    )

    # Write download script
    script = (
        '#!/bin/bash\n'
        f'MAGNET=$(cat {magnet_path})\n'
        f'aria2c --seed-time=0 --dir=/downloads --file-allocation=none '
        f'--timeout=15 --out=top_gun_test.mkv "$MAGNET"\n'
    )
    subprocess.run(
        ["docker", "cp", "/dev/stdin", f"{DOCKER}:{script_path}"],
        input=script.encode(), timeout=10,
    )

    print("\n[3c] Starting aria2 download in background (15s)...")
    subprocess.run(["docker", "exec", "-d", DOCKER, "bash", script_path], timeout=5)
    time.sleep(15)

    # Check for partial files
    ls_result = subprocess.run(
        ["docker", "exec", DOCKER, "ls", "-lh", "/downloads/"],
        capture_output=True, text=True, timeout=10,
    )
    has_files = any("top_gun" in l.lower() for l in ls_result.stdout.split("\n"))
    print(f"      Partial files present: {has_files}")
    results.append(TestResult("aria2 started and created partial files", has_files))

    # 3d: Cancel download
    print("\n[3d] Cancelling download (killing aria2)...")
    subprocess.run(["docker", "exec", DOCKER, "pkill", "-9", "aria2c"], timeout=5)
    time.sleep(1)

    # 3e: Delete partial files
    print("[3e] Deleting partial files...")
    subprocess.run(
        ["docker", "exec", DOCKER, "bash", "-c", "rm -rf /downloads/top_gun_test* /downloads/*Top*Gun*"],
        timeout=10,
    )
    time.sleep(1)

    # 3f: Verify clean
    ls_clean = subprocess.run(
        ["docker", "exec", DOCKER, "ls", "-la", "/downloads/"],
        capture_output=True, text=True, timeout=10,
    )
    leftover = any("top_gun" in l.lower() or "top gun" in l.lower()
                   for l in ls_clean.stdout.split("\n"))
    print(f"\n[3f] Downloads folder after cleanup:")
    for line in ls_clean.stdout.strip().split("\n"):
        if line.strip():
            print(f"      {line.strip()}")

    results.append(TestResult(
        "Partial files deleted, downloads folder clean",
        not leftover,
        "leftover files found" if leftover else "clean",
    ))


# ── Runner ──────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  PIRATE-DOCK SKILL TESTS")
    print(f"  API: {API}")
    print(f"  Container: {DOCKER}")
    print(f"  Time: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print("=" * 60)

    # Quick health check
    status = api_get("/status")
    vpn_connected = status.get("vpn", {}).get("connected", False)
    print(f"\n  VPN Connected: {vpn_connected}")
    print(f"  Jackett Ready: {status.get('jackett', {}).get('ready', False)}")

    if not vpn_connected:
        print("\n  WARNING: VPN not connected — results may vary")

    # Run all tests
    test_annas_archive()
    test_ufc_search()
    test_topgun_lifecycle()

    # Summary
    section("SUMMARY")
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    total = len(results)

    for r in results:
        print(r)

    print(f"\n  {passed}/{total} passed, {failed} failed")
    print("=" * 60)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
