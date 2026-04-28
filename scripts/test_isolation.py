#!/usr/bin/env python3
"""
Host network isolation tests for pirate-dock.

These tests enforce the core safety invariant:
  pirate-dock MUST NEVER modify the host's iptables or block host connectivity,
  regardless of what NordVPN does inside the container.

Root cause history (2026-04-17):
  pirate-dock was using network_mode: host with CAP_NET_ADMIN. NordVPN's killswitch
  set the host's iptables OUTPUT policy to DROP, with VPN bootstrap rules incorrectly
  tied to eth0 (which is unplugged). This blocked Discord, GitHub, and all external
  connectivity from the Pi for ~12 hours. Fix: bridge networking isolates NordVPN's
  iptables to the container's own network namespace.

These tests will FAIL immediately if network_mode: host is re-introduced, acting as
a regression guard for the entire class of problem.

Run from the project root (NOT inside the container):
    python3 scripts/test_isolation.py
    pytest scripts/test_isolation.py -v

Requirements: docker, sudo (for iptables inspection), curl
"""

import json
import subprocess
import sys
import time
import unittest

PROJECT_DIR = "/home/djmcnay/Documents/GitHub/pirate-dock"
CONTAINER = "pirate-dock"
API_URL = "http://127.0.0.1:9876"

# External hosts that must remain reachable from the Pi at all times.
CANARY_HOSTS = ["discord.com", "github.com", "google.com"]

# Time to wait (seconds) for NordVPN to initialise and apply its iptables inside
# the container. Long enough to catch a delayed killswitch activation.
VPN_INIT_WAIT = 35


# ── Helpers ───────────────────────────────────────────────────────────────────


def sh(cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def host_output_policy() -> str:
    """Return the iptables-legacy OUTPUT chain policy: 'ACCEPT' or 'DROP'."""
    r = sh("sudo iptables-legacy -L OUTPUT -n 2>/dev/null | head -1")
    if "policy DROP" in r.stdout:
        return "DROP"
    # Also check nft-backed iptables in case legacy isn't in use
    r2 = sh("sudo iptables -L OUTPUT -n 2>/dev/null | head -1")
    if "policy DROP" in r2.stdout:
        return "DROP"
    return "ACCEPT"


def nordvpn_rules_on_host() -> list[str]:
    """Return any NordVPN killswitch iptables rules found on the host (should be none)."""
    indicators = ["nordlynx", "tun+", "tap+"]
    found = []
    for table_cmd in [
        "sudo iptables-legacy -L -n 2>/dev/null",
        "sudo iptables -L -n 2>/dev/null",
    ]:
        r = sh(table_cmd)
        for line in r.stdout.splitlines():
            if any(ind in line for ind in indicators):
                found.append(line.strip())
    return found


def container_network_mode() -> str:
    r = sh(f"docker inspect {CONTAINER} --format '{{{{.HostConfig.NetworkMode}}}}'")
    return r.stdout.strip()


def container_is_running() -> bool:
    r = sh(f"docker ps --filter name=^{CONTAINER}$ --format '{{{{.Names}}}}'")
    return CONTAINER in r.stdout


def can_reach(host: str, timeout: int = 10) -> bool:
    r = sh(f"curl -s -o /dev/null -w '%{{http_code}}' --max-time {timeout} https://{host}")
    code = r.stdout.strip().strip("'")
    return code.isdigit() and 100 <= int(code) < 500


def start_container(wait_for_api: int = 60) -> bool:
    """Start the container and wait for API to respond. Returns True on success."""
    r = sh(f"cd {PROJECT_DIR} && docker compose up -d", timeout=30)
    if r.returncode != 0:
        return False
    for _ in range(wait_for_api):
        r = sh(f"curl -s --max-time 2 {API_URL}/status")
        if r.returncode == 0 and r.stdout.strip():
            return True
        time.sleep(1)
    return False


def stop_container():
    sh(f"cd {PROJECT_DIR} && docker compose down", timeout=30)
    time.sleep(3)


# ── Test class 1: baseline (container NOT running) ────────────────────────────


class TestBaseline(unittest.TestCase):
    """
    Sanity checks before the container starts.
    These tests verify the host is healthy before we run isolation tests.
    """

    @classmethod
    def setUpClass(cls):
        stop_container()

    def test_output_policy_is_accept(self):
        """Host OUTPUT iptables policy is ACCEPT before container starts."""
        self.assertEqual(host_output_policy(), "ACCEPT",
            "Host OUTPUT policy is already DROP before container starts — "
            "something outside pirate-dock has broken host networking.")

    def test_no_nordvpn_rules_before_start(self):
        """No NordVPN killswitch rules on host before container starts."""
        rules = nordvpn_rules_on_host()
        self.assertEqual(rules, [],
            f"NordVPN rules already present on host before container start: {rules}")

    def test_canary_hosts_reachable(self):
        """Canary hosts (Discord, GitHub, Google) are reachable from the Pi."""
        for host in CANARY_HOSTS:
            with self.subTest(host=host):
                self.assertTrue(can_reach(host),
                    f"Cannot reach {host} before container starts — "
                    "pre-existing network issue unrelated to pirate-dock.")


# ── Test class 2: isolation while container runs ──────────────────────────────


class TestIsolationWhileRunning(unittest.TestCase):
    """
    Core isolation invariants: pirate-dock running MUST NOT affect the host.

    A failure here means pirate-dock can break Pi-wide connectivity (the incident
    of 2026-04-17). These tests must pass before any changes are deployed.
    """

    @classmethod
    def setUpClass(cls):
        stop_container()
        started = start_container()
        if not started:
            raise RuntimeError(
                "Container did not start or API did not become ready. "
                "Run `docker logs pirate-dock` to debug."
            )
        # Wait for NordVPN to initialise and apply its internal iptables.
        print(f"\n  [isolation] Waiting {VPN_INIT_WAIT}s for NordVPN to initialise...",
              flush=True)
        time.sleep(VPN_INIT_WAIT)

    @classmethod
    def tearDownClass(cls):
        stop_container()

    # ── Network mode guard ──────────────────────────────────────────────────

    def test_container_uses_bridge_not_host_networking(self):
        """
        Container MUST NOT use network_mode: host.

        Host networking shares the host's network namespace, meaning NordVPN's
        iptables killswitch is applied directly to the Pi. Bridge networking
        confines NordVPN to the container's own namespace.
        """
        mode = container_network_mode()
        self.assertNotEqual(mode, "host",
            "CRITICAL: container is using host networking. "
            "NordVPN's killswitch CAN modify host iptables. "
            "Remove network_mode: host from docker-compose.yml immediately.")

    # ── Host iptables invariants ────────────────────────────────────────────

    def test_host_output_policy_unchanged(self):
        """
        Host OUTPUT iptables policy must remain ACCEPT while container runs.

        If this fails, pirate-dock is leaking NordVPN's killswitch to the host,
        which will block Discord, GitHub, and all non-local connectivity from the Pi.
        """
        policy = host_output_policy()
        self.assertEqual(policy, "ACCEPT",
            "CRITICAL: host OUTPUT policy is DROP while pirate-dock runs. "
            "NordVPN killswitch has leaked to host — all Pi internet connectivity is broken.")

    def test_no_nordvpn_killswitch_on_host(self):
        """
        No NordVPN killswitch interface rules (nordlynx+, tun+) on host iptables.

        These rules appearing on the host indicates network namespace leakage.
        """
        rules = nordvpn_rules_on_host()
        self.assertEqual(rules, [],
            f"NordVPN killswitch rules found on host while container runs: {rules}\n"
            "This means network_mode: host is active or namespace isolation is broken.")

    # ── Host connectivity invariants ────────────────────────────────────────

    def test_host_can_reach_discord(self):
        """Host Pi can reach discord.com while pirate-dock runs."""
        self.assertTrue(can_reach("discord.com"),
            "CRITICAL: cannot reach discord.com while pirate-dock runs. "
            "Minty cannot respond on Discord. Check host iptables immediately.")

    def test_host_can_reach_github(self):
        """Host Pi can reach github.com (and thus run hermes update) while pirate-dock runs."""
        self.assertTrue(can_reach("github.com"),
            "CRITICAL: cannot reach github.com while pirate-dock runs.")

    def test_host_can_reach_google(self):
        """Host Pi can reach google.com while pirate-dock runs."""
        self.assertTrue(can_reach("google.com"),
            "Cannot reach google.com while pirate-dock runs.")

    # ── API accessibility ───────────────────────────────────────────────────

    def test_api_accessible_via_published_port(self):
        """API is directly reachable at localhost:9876 — no docker exec needed."""
        r = sh(f"curl -s --max-time 10 {API_URL}/status")
        self.assertEqual(r.returncode, 0,
            "curl to localhost:9876 failed — port not published or container not listening.")
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            self.fail(f"API /status did not return valid JSON: {r.stdout[:300]}")
        self.assertIn("connected", data, "API /status missing VPN connection state")
        self.assertIn("display_url", data, "API /status missing xpra display URL")

    def test_jackett_accessible_via_published_port(self):
        """Jackett UI is directly reachable at localhost:9118."""
        r = sh("curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:9118")
        code = r.stdout.strip().strip("'")
        self.assertTrue(code.isdigit() and int(code) < 500,
            f"Jackett on localhost:9118 returned unexpected status: {code}")

    def test_container_is_running(self):
        """Container is confirmed running (sanity check for other tests)."""
        self.assertTrue(container_is_running())


# ── Test class 3: cleanup after stop ─────────────────────────────────────────


class TestCleanupAfterStop(unittest.TestCase):
    """
    Stopping the container must leave the host completely clean.
    No iptables debris, no broken connectivity.
    """

    @classmethod
    def setUpClass(cls):
        # Start, wait for VPN init, then stop cleanly
        stop_container()
        start_container()
        print(f"\n  [cleanup] Waiting {VPN_INIT_WAIT}s for NordVPN to initialise...",
              flush=True)
        time.sleep(VPN_INIT_WAIT)
        stop_container()

    def test_output_policy_after_stop(self):
        """Host OUTPUT policy is ACCEPT after container stops."""
        policy = host_output_policy()
        self.assertEqual(policy, "ACCEPT",
            "Host OUTPUT policy is DROP after container stops — "
            "iptables rules were not cleaned up on container exit.")

    def test_no_nordvpn_rules_after_stop(self):
        """No NordVPN killswitch rules remain on host after container stops."""
        rules = nordvpn_rules_on_host()
        self.assertEqual(rules, [],
            f"NordVPN rules remain on host after container stops: {rules}")

    def test_canary_hosts_reachable_after_stop(self):
        """Canary hosts are reachable after container stops."""
        for host in CANARY_HOSTS:
            with self.subTest(host=host):
                self.assertTrue(can_reach(host),
                    f"Cannot reach {host} after container stops — "
                    "iptables were not properly restored.")


# ── Runner ────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    print("=" * 70)
    print("  PIRATE-DOCK HOST ISOLATION TESTS")
    print("  Verifying container cannot affect host networking.")
    print("=" * 70)
    unittest.main(verbosity=2, exit=True)
