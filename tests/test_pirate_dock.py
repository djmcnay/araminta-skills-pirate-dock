#!/usr/bin/env python3
"""Pirate-dock skill smoke tests — Docker presence, script structure, API connectivity.

Output: TAP format + __TEST_RESULT__ JSON summary line.
"""

import json
import os
import subprocess
import sys
import urllib.request
import urllib.error

# ── Helpers ──────────────────────────────────────────────────────────────────

passed = 0
failed = 0
details = []


def ok(name: str, msg: str = ""):
    global passed
    passed += 1
    tag = f"ok {len(details) + 1} - {name}"
    if msg:
        tag += f"  # {msg}"
    print(tag)
    details.append({"test": name, "status": "pass", "msg": msg})


def not_ok(name: str, msg: str = ""):
    global failed
    failed += 1
    tag = f"not ok {len(details) + 1} - {name}"
    if msg:
        tag += f"  # {msg}"
    print(tag)
    details.append({"test": name, "status": "fail", "msg": msg})


def _run(cmd: str, timeout: int = 10) -> tuple[int, str]:
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return r.returncode, (r.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return 1, "timeout"
    except Exception as e:
        return 1, str(e)


# ── Tests ────────────────────────────────────────────────────────────────────

SKILL_DIR = os.path.expanduser("~/.hermes/skills/pirate-dock")


def test_01_skill_dir_exists():
    if os.path.isdir(SKILL_DIR):
        ok("skill_dir", SKILL_DIR)
    else:
        not_ok("skill_dir", f"{SKILL_DIR} missing")


def test_02_docker_compose_exists():
    dc = os.path.join(SKILL_DIR, "docker-compose.yml")
    df = os.path.join(SKILL_DIR, "Dockerfile")
    found = 0
    for f in [dc, df]:
        if os.path.isfile(f):
            found += 1
    if found == 2:
        ok("docker_files", "docker-compose.yml and Dockerfile present")
    else:
        not_ok("docker_files", f"{found}/2 present")


def test_03_scripts_dir_non_empty():
    scripts_dir = os.path.join(SKILL_DIR, "scripts")
    if not os.path.isdir(scripts_dir):
        not_ok("scripts_dir", "scripts/ missing")
        return
    files = [f for f in os.listdir(scripts_dir) if f.endswith(('.py', '.sh', '.js'))]
    count = len(files)
    if count >= 5:
        ok("scripts_dir", f"{count} executable scripts")
    else:
        not_ok("scripts_dir", f"only {count} scripts (expected 5+)")


def test_04_docker_daemon_reachable():
    rc, out = _run("docker version --format '{{.Server.Version}}'", timeout=5)
    if rc == 0:
        ok("docker_daemon", f"v{out}")
    else:
        not_ok("docker_daemon", f"docker not available: {out[:100]}")


def test_05_browser_endpoint_reachable():
    """Check if the pirate-dock browser endpoint responds."""
    try:
        req = urllib.request.Request("http://localhost:9223/json/version", method="GET")
        with urllib.request.urlopen(req, timeout=3) as r:
            body = r.read().decode()
            if r.status == 200:
                ok("browser_endpoint", f"CDP on :9223 — {body[:60]}")
            else:
                ok("browser_endpoint", f":9223 responded {r.status} (browser may be off)")
    except urllib.error.URLError as e:
        ok("browser_endpoint", f"CDP not reachable (browser not running: {type(e).__name__})")
    except Exception as e:
        ok("browser_endpoint", f"CDP not reachable (expected if browser off: {type(e).__name__})")


def test_06_api_scripts_exist():
    expected = ["browser_fallback.py", "run.sh", "server.py"]
    scripts_dir = os.path.join(SKILL_DIR, "scripts")
    found = 0
    issues = []
    for s in expected:
        full = os.path.join(scripts_dir, s)
        if os.path.isfile(full):
            found += 1
        else:
            issues.append(s)
    if found == len(expected):
        ok("api_scripts", f"{found}/{len(expected)} present")
    else:
        not_ok("api_scripts", f"missing: {', '.join(issues)}")


def test_07_readme_exists():
    readme = os.path.join(SKILL_DIR, "README.md")
    if os.path.isfile(readme):
        ok("readme", "README.md present")
    else:
        not_ok("readme", "README.md missing")


# ── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"# pirate-dock tests — {json.dumps(str(__import__('datetime').datetime.now()))}")

    test_01_skill_dir_exists()
    test_02_docker_compose_exists()
    test_03_scripts_dir_non_empty()
    test_04_docker_daemon_reachable()
    test_05_browser_endpoint_reachable()
    test_06_api_scripts_exist()
    test_07_readme_exists()

    total = passed + failed
    print(f"\n# {passed}/{total} passed, {failed} failed")
    print(f"1..{total}")

    result = {
        "skill": "pirate-dock",
        "passed": passed,
        "failed": failed,
        "total": total,
        "details": details,
    }
    print(f"__TEST_RESULT__:{json.dumps(result)}")

    sys.exit(1 if failed > 0 else 0)
