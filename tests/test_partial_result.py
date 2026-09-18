import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from fb_ads_scraper.cli import main
from fb_ads_scraper.scraper import AdLibraryScraper


class _Page:
    url = "https://www.facebook.com/ads/library/"

    def __init__(self, error=None):
        self.error = error

    def on(self, *_): pass
    def goto(self, *_, **__): pass
    def eval_on_selector_all(self, *_): return []
    def wait_for_timeout(self, *_): pass

    def evaluate(self, *_):
        if self.error:
            raise self.error


class _Browser:
    def __init__(self, page): self.page = page
    def new_context(self, **_): return type("Context", (), {"new_page": lambda _: self.page})()
    def close(self): pass


class _PlaywrightContext:
    def __init__(self, page): self.page = page
    def __enter__(self):
        chromium = type("Chromium", (), {"launch": lambda *_args, **_kwargs: _Browser(self.page)})()
        return type("Playwright", (), {"chromium": chromium})()
    def __exit__(self, *_): return False


class PartialResultTests(unittest.TestCase):
    def _scraper(self, timeout=3600):
        scraper = AdLibraryScraper("https://www.facebook.com/ads/library/", timeout=timeout)
        scraper.raw_ads = {"ad-1": {"ad_archive_id": "ad-1"}}
        scraper.raw_ads_observed = 1
        return scraper

    def test_deadline_preserves_collected_ads_as_partial(self):
        scraper = self._scraper()
        with patch("fb_ads_scraper.scraper.sync_playwright", return_value=_PlaywrightContext(_Page())), patch("fb_ads_scraper.scraper.parse_ad", side_effect=lambda ad: ad), patch("fb_ads_scraper.scraper.time.monotonic", side_effect=[0, 3600, 3601]):
            ads = scraper.run()
        self.assertEqual(ads, [{"ad_archive_id": "ad-1"}])
        self.assertFalse(scraper.complete)
        self.assertEqual(scraper.stop_reason, "TIME_LIMIT_REACHED")
        self.assertEqual(scraper.raw_ads_observed, 1)

    def test_completed_collection_remains_complete(self):
        scraper = self._scraper()
        scraper.max_results = 1
        with patch("fb_ads_scraper.scraper.sync_playwright", return_value=_PlaywrightContext(_Page())), patch("fb_ads_scraper.scraper.parse_ad", side_effect=lambda ad: ad), patch("fb_ads_scraper.scraper.time.monotonic", side_effect=[0, 1]):
            scraper.run()
        self.assertTrue(scraper.complete)
        self.assertIsNone(scraper.stop_reason)

    def test_cli_writes_partial_envelope(self):
        scraper = type("Scraper", (), {"complete": False, "stop_reason": "TIME_LIMIT_REACHED", "collection_duration_seconds": 3600, "raw_ads_observed": 2, "run": lambda self: [{"ad_archive_id": "ad-1", "page_id": "page"}]})()
        with tempfile.TemporaryDirectory() as directory, patch("fb_ads_scraper.cli.AdLibraryScraper", return_value=scraper):
            self.assertEqual(main(["--keyword", "x", "--format", "json", "--result-envelope", "--output", directory]), 0)
            result = json.loads((Path(directory) / "radar-result.json").read_text())
        self.assertEqual(result["ads"][0]["ad_archive_id"], "ad-1")
        self.assertFalse(result["complete"])
        self.assertEqual(result["stopReason"], "TIME_LIMIT_REACHED")

    def test_cli_rejects_empty_partial_result(self):
        scraper = type("Scraper", (), {"complete": False, "stop_reason": "TIME_LIMIT_REACHED", "collection_duration_seconds": 3600, "raw_ads_observed": 0, "run": lambda self: []})()
        with tempfile.TemporaryDirectory() as directory, patch("fb_ads_scraper.cli.AdLibraryScraper", return_value=scraper):
            self.assertEqual(main(["--keyword", "x", "--result-envelope", "--output", directory]), 1)
            self.assertFalse((Path(directory) / "radar-result.json").exists())

    def test_pagination_exception_remains_failure_even_with_ads(self):
        scraper = self._scraper()
        with patch("fb_ads_scraper.scraper.sync_playwright", return_value=_PlaywrightContext(_Page(RuntimeError("browser boom")))), patch("fb_ads_scraper.scraper.time.monotonic", side_effect=[0, 1]):
            with self.assertRaisesRegex(RuntimeError, "falha durante paginação"):
                scraper.run()
        self.assertTrue(scraper.complete)
        self.assertIsNone(scraper.stop_reason)
