import unittest
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from fb_ads_scraper.cli import main
from fb_ads_scraper.scraper import AdLibraryScraper, parse_total_results


class _Page:
    url = "https://www.facebook.com/ads/library/"

    def __init__(self, error=None):
        self.error = error
        self.reads = []
        self.scrolls = 0

    def on(self, *_): pass
    def goto(self, *_, **__): pass
    def eval_on_selector_all(self, *_): return []
    def wait_for_timeout(self, *_): pass

    def evaluate(self, *_):
        self.scrolls += 1
        if self.error:
            raise self.error

    def locator(self, *_):
        page = self
        return type("Locator", (), {"inner_text": lambda *_args, **_kwargs: page.reads.pop(0) if page.reads else "~15 resultados"})()


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
    def test_module_propagates_cli_exit_code(self):
        result = subprocess.run(
            [sys.executable, "-m", "fb_ads_scraper"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)

    def test_total_results_counter(self):
        self.assertEqual(parse_total_results("Biblioteca\n~15 resultados\nFiltros"), 15)
        self.assertEqual(parse_total_results("1.234 resultados"), 1234)
        self.assertIsNone(parse_total_results("3 anúncios observados"))

    def test_total_only_reads_counter_and_skips_pagination(self):
        page = _Page()
        scraper = AdLibraryScraper("https://www.facebook.com/ads/library/", total_only=True)
        with patch("fb_ads_scraper.scraper.sync_playwright", return_value=_PlaywrightContext(page)):
            self.assertEqual(scraper.run(), [])
        self.assertEqual(scraper.total_results, 15)
        self.assertEqual(scraper.total_results_source, "META_RESULT_COUNTER")
        self.assertEqual(page.scrolls, 0)

    def test_total_only_polls_until_counter_appears(self):
        page = _Page()
        page.reads = ["Carregando", "", "15 resultados"]
        scraper = AdLibraryScraper("https://www.facebook.com/ads/library/", total_only=True)
        with patch("fb_ads_scraper.scraper.sync_playwright", return_value=_PlaywrightContext(page)):
            self.assertEqual(scraper.run(), [])
        self.assertEqual(scraper.total_results, 15)
        self.assertEqual(page.scrolls, 0)

    def test_total_only_fails_without_counter_and_never_scrolls(self):
        page = _Page()
        page.reads = ["Carregando"] * 100
        scraper = AdLibraryScraper("https://www.facebook.com/ads/library/", total_only=True)
        with patch("fb_ads_scraper.scraper.sync_playwright", return_value=_PlaywrightContext(page)), \
             patch("fb_ads_scraper.scraper.time.monotonic", side_effect=[0, *([1] * 15), 16, 16]):
            with self.assertRaisesRegex(RuntimeError, "TOTAL_RESULTS_NOT_FOUND"):
                scraper.run()
        self.assertIsNone(scraper.total_results)
        self.assertEqual(page.scrolls, 0)
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
        scraper = type("Scraper", (), {"complete": False, "stop_reason": "TIME_LIMIT_REACHED", "collection_duration_seconds": 3600, "raw_ads_observed": 2, "total_results": 15, "total_results_source": "META_RESULT_COUNTER", "run": lambda self: [{"ad_archive_id": "ad-1", "page_id": "page"}]})()
        with tempfile.TemporaryDirectory() as directory, patch("fb_ads_scraper.cli.AdLibraryScraper", return_value=scraper):
            self.assertEqual(main(["--keyword", "x", "--format", "json", "--result-envelope", "--output", directory]), 0)
            result = json.loads((Path(directory) / "radar-result.json").read_text())
        self.assertEqual(result["ads"][0]["ad_archive_id"], "ad-1")
        self.assertFalse(result["complete"])
        self.assertEqual(result["stopReason"], "TIME_LIMIT_REACHED")
        self.assertEqual(result["totalResults"], 15)

    def test_cli_rejects_empty_partial_result(self):
        scraper = type("Scraper", (), {"complete": False, "stop_reason": "TIME_LIMIT_REACHED", "collection_duration_seconds": 3600, "raw_ads_observed": 0, "total_results": None, "total_results_source": None, "run": lambda self: []})()
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
