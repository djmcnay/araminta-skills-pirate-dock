"""
Unit tests for pirate-dock browser fallback.

Run from the project root:
  python3 scripts/test_browser.py

These tests cover the current architecture: Playwright/Chromium launches inside
the container and the user-facing browser is exposed through xpra/Tailscale.
"""

import asyncio
import importlib
import json
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


SAMPLE_BOOK_PAGE = """
<html>
<head><title>Test Book</title></head>
<body>
<h1 class="md5card-title">The Art of Physics</h1>
<a href="/slow_download/abc123def456789012345678901234f0">
  Slow Partner Server Download
</a>
<a href="https://example.com/file.pdf">Download PDF</a>
</body>
</html>
"""

SAMPLE_CAPTCHA_PAGE = """
<html>
<head><title>Just a moment...</title></head>
<body>
<div id="captcha-container">
  DDoS-Guard verification challenge
  <p>Please verify you are human</p>
  <p>Are you human?</p>
</div>
</body>
</html>
"""


class FakeLocator:
    def __init__(self, count=0):
        self._count = count

    async def count(self):
        return self._count


class FakePage:
    def __init__(self, title="", body="", url="https://example.test/book", locator_counts=None):
        self._title = title
        self._body = body
        self.url = url
        self.locator_counts = locator_counts or {}

    async def title(self):
        return self._title

    async def evaluate(self, script):
        if "document.body.innerText" in script:
            return self._body
        if "document.querySelectorAll('a[href]')" in script:
            links = self.locator_counts.get("download_links", [])
            return json.dumps(links)
        return ""

    def locator(self, selector):
        if "iframe" in selector or "h-captcha" in selector or "g-recaptcha" in selector:
            return FakeLocator(self.locator_counts.get("captcha", 0))
        if "countdown" in selector:
            return FakeLocator(self.locator_counts.get("countdown", 0))
        return FakeLocator(0)


class TestChallengeDetection(unittest.TestCase):
    def test_ddos_guard_title_detected(self):
        from browser_fallback import _detect_challenge

        page = FakePage(title="DDoS-Guard", body="Checking your browser")
        result = asyncio.run(_detect_challenge(page))

        self.assertEqual(result["challenge"], "ddos_guard_js")

    def test_manual_challenge_detected(self):
        from browser_fallback import _detect_challenge

        page = FakePage(
            title="DDoS-Guard",
            body="Please complete the manual check to continue",
        )
        result = asyncio.run(_detect_challenge(page))

        self.assertEqual(result["challenge"], "ddos_guard_manual")

    def test_captcha_frame_detected(self):
        from browser_fallback import _detect_challenge

        page = FakePage(title="Anna's Archive", body="", locator_counts={"captcha": 1})
        result = asyncio.run(_detect_challenge(page))

        self.assertEqual(result["challenge"], "captcha")

    def test_clean_download_links_detected(self):
        from browser_fallback import _detect_challenge

        links = [{"text": "Download PDF", "url": "https://example.test/book.pdf"}]
        page = FakePage(
            title="Anna's Archive",
            body="A normal book page",
            locator_counts={"download_links": links},
        )
        result = asyncio.run(_detect_challenge(page))

        self.assertEqual(result["challenge"], "none")
        self.assertEqual(result["download_links"], links)

    def test_countdown_detected(self):
        from browser_fallback import _detect_challenge

        page = FakePage(title="Download", body="Please wait", locator_counts={"countdown": 1})
        result = asyncio.run(_detect_challenge(page))

        self.assertEqual(result["challenge"], "countdown")


class TestBrowserDownload(unittest.TestCase):
    def test_browser_download_reports_missing_playwright(self):
        from browser_fallback import browser_download

        with patch("browser_fallback._check_playwright", return_value=False):
            result = asyncio.run(browser_download("abc123def456789012345678901234f0"))

        self.assertEqual(result["status"], "error")
        self.assertIn("Playwright not installed", result["message"])
        self.assertEqual(result["md5"], "abc123def456789012345678901234f0")

    def test_no_links_result_includes_display_url(self):
        import browser_fallback

        class FakePlaywrightContext:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        fake_playwright_api = types.SimpleNamespace(
            async_playwright=lambda: FakePlaywrightContext(),
            TimeoutError=TimeoutError,
        )

        fake_link_locator = MagicMock()
        fake_link_locator.all = AsyncMock(return_value=[])

        page = MagicMock()
        page.goto = AsyncMock()
        page.locator.return_value = fake_link_locator

        browser = MagicMock()
        browser.close = AsyncMock()

        async def fake_launch(_p, headless=True):
            return browser, page

        with patch("browser_fallback._check_playwright", return_value=True), \
             patch.dict(sys.modules, {
                 "playwright": types.SimpleNamespace(),
                 "playwright.async_api": fake_playwright_api,
             }), \
             patch("browser_fallback._launch_browser", side_effect=fake_launch):
            result = asyncio.run(browser_fallback.browser_download(
                "abc123def456789012345678901234f0"
            ))

        self.assertEqual(result["status"], "no_links_found")
        self.assertIn(browser_fallback.DISPLAY_URL, result["message"])
        browser.close.assert_awaited_once()


class TestBrowserStatus(unittest.TestCase):
    def test_browser_status_missing_playwright_includes_display_url(self):
        import browser_fallback

        with patch("browser_fallback._check_playwright", return_value=False):
            result = asyncio.run(browser_fallback.browser_status())

        self.assertFalse(result["available"])
        self.assertEqual(result["display_url"], browser_fallback.DISPLAY_URL)

    def test_display_url_env_default(self):
        if "browser_fallback" in sys.modules:
            del sys.modules["browser_fallback"]
        os.environ.pop("DISPLAY_URL", None)

        import browser_fallback

        self.assertEqual(browser_fallback.DISPLAY_URL, "https://araminta.taild3f7b9.ts.net:8443/pirate/")

    def test_display_url_env_override(self):
        os.environ["DISPLAY_URL"] = "https://custom.example/"
        if "browser_fallback" in sys.modules:
            del sys.modules["browser_fallback"]

        import browser_fallback

        self.assertEqual(browser_fallback.DISPLAY_URL, "https://custom.example/")
        os.environ.pop("DISPLAY_URL", None)
        importlib.reload(browser_fallback)


class TestServerIntegration(unittest.TestCase):
    def test_has_browser_fallback_flag(self):
        try:
            from server import HAS_BROWSER_FALLBACK
        except ImportError as exc:
            self.skipTest(f"server.py dependencies not available: {exc}")

        self.assertTrue(HAS_BROWSER_FALLBACK)


class TestDownloadAnnasAutoFallback(unittest.TestCase):
    def test_headless_blocked_detection_markers(self):
        captcha_markers = [
            "captcha", "ddos-guard", "challenge",
            "just a moment", "please verify", "are you human",
        ]
        for marker in captcha_markers:
            self.assertIn(marker, SAMPLE_CAPTCHA_PAGE.lower())

    def test_clean_page_no_captcha_markers(self):
        captcha_markers = [
            "captcha", "ddos-guard", "challenge",
            "just a moment", "please verify", "are you human",
        ]
        for marker in captcha_markers:
            self.assertNotIn(marker, SAMPLE_BOOK_PAGE.lower())


if __name__ == "__main__":
    print("=" * 60)
    print("Pirate Dock - Browser Fallback Unit Tests")
    print("=" * 60)
    unittest.main(verbosity=2)
