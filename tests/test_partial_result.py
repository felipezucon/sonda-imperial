import unittest
import json
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from fb_ads_scraper.cli import main
from fb_ads_scraper.scraper import (AdLibraryScraper, _find_global_load_more,
                                    has_meta_empty_state, parse_total_results)


class _Page:
    url = "https://www.facebook.com/ads/library/"

    def __init__(self, error=None):
        self.error = error
        self.reads = []
        self.default_body = "~15 resultados"
        self.scrolls = 0
        self.button_metadata = []
        self.buttons = []

    def on(self, *_): pass
    def goto(self, *_, **__): pass
    def close(self): pass
    def eval_on_selector_all(self, *_): return []
    def wait_for_timeout(self, *_): pass

    def evaluate(self, *_):
        self.scrolls += 1
        if self.error:
            raise self.error

    def locator(self, *_):
        page = self
        if _ and _[0] != "body":
            class Buttons:
                def evaluate_all(self, *_args): return page.button_metadata
                def nth(self, index): return page.buttons[index]
            return Buttons()
        return type("Locator", (), {"inner_text": lambda *_args, **_kwargs: page.reads.pop(0) if page.reads else page.default_body})()


class _LoadMoreButton:
    def __init__(self, click=None): self.click_action = click; self.clicks = 0
    def click(self, **_):
        self.clicks += 1
        if self.click_action: self.click_action()


class _Browser:
    def __init__(self, page): self.page = page; self.closed = False; self.context_options = []; self.connected = None; self.contexts = []
    def new_context(self, **options):
        self.context_options.append(options)
        return type("Context", (), {"new_page": lambda _: self.page})()
    def new_page(self): return self.page
    def close(self): self.closed = True


class _PlaywrightContext:
    def __init__(self, page): self.page = page
    def __enter__(self):
        self.browser = _Browser(self.page)
        self.browser.contexts = [self.browser]
        harness = self
        class Chromium:
            def launch(self, *_args, **_kwargs): return harness.browser
            def launch_persistent_context(self, user_data_dir, **options):
                harness.browser.profile = user_data_dir
                harness.browser.profile_options = options
                return harness.browser
            def connect_over_cdp(self, endpoint, **options):
                harness.browser.connected = (endpoint, options)
                return harness.browser
        chromium = Chromium()
        return type("Playwright", (), {"chromium": chromium})()
    def __exit__(self, *_): return False


class PartialResultTests(unittest.TestCase):
    def setUp(self):
        observe = patch("fb_ads_scraper.scraper.PAGINATION_OBSERVE_SECONDS", 0)
        observe.start()
        self.addCleanup(observe.stop)

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
        self.assertEqual(parse_total_results("15 resultados"), 15)
        self.assertEqual(parse_total_results("0 resultados"), 0)
        self.assertEqual(parse_total_results("1.234 resultados"), 1234)
        self.assertIsNone(parse_total_results("3 anúncios observados"))

    def test_http_error_is_not_interpreted_as_meta_empty_state(self):
        page = _Page()
        page.goto = lambda *_, **__: SimpleNamespace(status=403)
        context = _PlaywrightContext(page)
        scraper = AdLibraryScraper("https://www.facebook.com/ads/library/")
        with patch("fb_ads_scraper.scraper.sync_playwright", return_value=context):
            with self.assertRaisesRegex(RuntimeError, "META_HTTP_ERROR: HTTP 403"):
                scraper.run()
        self.assertFalse(scraper.empty_state_detected)
        self.assertTrue(context.browser.closed)

    def test_meta_empty_state_is_explicit_and_tolerates_spacing_and_case(self):
        self.assertTrue(has_meta_empty_state(
            "Nenhum anúncio corresponde aos seus critérios de pesquisa"
        ))
        self.assertTrue(has_meta_empty_state(
            " NENHUM   ANÚNCIO corresponde aos seus critérios de pesquisa "
        ))
        self.assertFalse(has_meta_empty_state("Não há anúncios disponíveis"))

    def test_total_only_reads_counter_and_skips_pagination(self):
        page = _Page()
        scraper = AdLibraryScraper("https://www.facebook.com/ads/library/", total_only=True)
        with patch("fb_ads_scraper.scraper.sync_playwright", return_value=_PlaywrightContext(page)):
            self.assertEqual(scraper.run(), [])
        self.assertEqual(scraper.total_results, 15)
        self.assertEqual(scraper.total_results_source, "META_RESULT_COUNTER")
        self.assertEqual(page.scrolls, 0)

    def test_native_user_agent_does_not_override_browser_context(self):
        page = _Page()
        context = _PlaywrightContext(page)
        scraper = AdLibraryScraper("https://www.facebook.com/ads/library/", max_results=1,
                                   native_user_agent=True)
        with patch("fb_ads_scraper.scraper.sync_playwright", return_value=context), \
             patch("fb_ads_scraper.scraper.parse_ad", side_effect=lambda ad: ad):
            scraper.raw_ads = {"ad-1": {"ad_archive_id": "ad-1"}}
            scraper.run()
        self.assertNotIn("user_agent", context.browser.context_options[0])

    def test_persistent_profile_is_used_only_when_supplied(self):
        page = _Page()
        context = _PlaywrightContext(page)
        with tempfile.TemporaryDirectory() as profile:
            scraper = AdLibraryScraper("https://www.facebook.com/ads/library/", max_results=1,
                                       native_user_agent=True, headful=True, profile_dir=profile,
                                       browser_channel="chrome")
            scraper.raw_ads = {"ad-1": {"ad_archive_id": "ad-1"}}
            with patch("fb_ads_scraper.scraper.sync_playwright", return_value=context), \
                 patch("fb_ads_scraper.scraper.parse_ad", side_effect=lambda ad: ad):
                scraper.run()
        self.assertEqual(context.browser.profile, profile)
        self.assertFalse(context.browser.profile_options["headless"])
        self.assertNotIn("user_agent", context.browser.profile_options)
        self.assertEqual(context.browser.profile_options["channel"], "chrome")

    def test_mining_connects_to_cdp_default_context_and_only_closes_its_page(self):
        page = _Page()
        context = _PlaywrightContext(page)
        scraper = AdLibraryScraper("https://www.facebook.com/ads/library/", max_results=1,
                                   cdp_endpoint="http://127.0.0.1:9222")
        scraper.raw_ads = {"ad-1": {"ad_archive_id": "ad-1"}}
        with patch("fb_ads_scraper.scraper.sync_playwright", return_value=context), \
             patch("fb_ads_scraper.scraper.parse_ad", side_effect=lambda ad: ad):
            self.assertEqual(scraper.run(), [{"ad_archive_id": "ad-1"}])
        self.assertEqual(context.browser.connected,
                         ("http://127.0.0.1:9222", {"timeout": 10_000}))
        self.assertTrue(context.browser.closed)
        self.assertFalse(hasattr(context.browser, "profile"))

    def test_global_load_more_detector_requires_one_visible_bottom_button_outside_cards_and_dialogs(self):
        page = _Page()
        page.button_metadata = [
            {"index": 0, "name": "ver mais", "visible": True, "enabled": True,
             "inDialog": True, "inArticle": False, "afterLastAd": True,
             "nearBottom": True, "pageAtBottom": True},
            {"index": 1, "name": "ver mais", "visible": True, "enabled": True,
             "inDialog": False, "inArticle": True, "afterLastAd": True,
             "nearBottom": True, "pageAtBottom": True},
            {"index": 2, "name": "ver mais", "visible": True, "enabled": True,
             "inDialog": False, "inArticle": False, "afterLastAd": True,
             "nearBottom": True, "pageAtBottom": True},
        ]
        page.buttons = [object(), object(), object()]
        self.assertIs(_find_global_load_more(page), page.buttons[2])
        page.button_metadata[2]["afterLastAd"] = False
        self.assertIsNone(_find_global_load_more(page))
        page.button_metadata[2]["afterLastAd"] = True
        page.button_metadata.append({"index": 3, "name": "ver mais", "visible": True,
                                     "enabled": True, "inDialog": False, "inArticle": False,
                                     "afterLastAd": True, "nearBottom": True,
                                     "pageAtBottom": True})
        page.buttons.append(object())
        self.assertIsNone(_find_global_load_more(page))

    def test_clicking_global_load_more_and_new_ads_continues_collection(self):
        page = _Page()
        scraper = AdLibraryScraper("https://www.facebook.com/ads/library/", max_results=2)
        scraper.raw_ads = {"ad-1": {"ad_archive_id": "ad-1"}}
        scraper.raw_ads_observed = 1
        page.button_metadata = [{"index": 0, "name": "ver mais", "visible": True,
                                 "enabled": True, "inDialog": False, "inArticle": False,
                                 "afterLastAd": True,
                                 "nearBottom": True, "pageAtBottom": True}]
        button = _LoadMoreButton(lambda: scraper.raw_ads.update(
            {"ad-2": {"ad_archive_id": "ad-2"}}))
        page.buttons = [button]
        with patch("fb_ads_scraper.scraper.sync_playwright", return_value=_PlaywrightContext(page)), \
             patch("fb_ads_scraper.scraper.parse_ad", side_effect=lambda ad: ad):
            ads = scraper.run()
        self.assertEqual(len(ads), 2)
        self.assertEqual(button.clicks, 1)
        self.assertEqual(page.scrolls, 1)
        self.assertTrue(scraper.complete)

    def test_lazy_growth_during_observation_window_needs_no_load_more_button(self):
        page = _Page()
        scraper = AdLibraryScraper("https://www.facebook.com/ads/library/", max_results=2)
        scraper.raw_ads = {"ad-1": {"ad_archive_id": "ad-1"}}
        scraper.raw_ads_observed = 1
        waits = []

        def delayed_growth(milliseconds):
            waits.append(milliseconds)
            if len(waits) > 16:
                scraper.raw_ads["ad-2"] = {"ad_archive_id": "ad-2"}

        page.wait_for_timeout = delayed_growth
        with patch("fb_ads_scraper.scraper.PAGINATION_OBSERVE_SECONDS", 3), \
             patch("fb_ads_scraper.scraper.sync_playwright", return_value=_PlaywrightContext(page)), \
             patch("fb_ads_scraper.scraper.parse_ad", side_effect=lambda ad: ad):
            ads = scraper.run()
        self.assertEqual(len(ads), 2)
        self.assertGreater(len(waits), 0)
        self.assertEqual(page.scrolls, 1)

    def test_repeated_load_more_without_growth_falls_back_to_stability_logic(self):
        page = _Page()
        scraper = AdLibraryScraper("https://www.facebook.com/ads/library/")
        scraper.raw_ads = {"ad-1": {"ad_archive_id": "ad-1"}}
        scraper.raw_ads_observed = 1
        page.button_metadata = [{"index": 0, "name": "ver mais", "visible": True,
                                 "enabled": True, "inDialog": False, "inArticle": False,
                                 "afterLastAd": True,
                                 "nearBottom": True, "pageAtBottom": True}]
        button = _LoadMoreButton()
        page.buttons = [button]
        with patch("fb_ads_scraper.scraper.sync_playwright", return_value=_PlaywrightContext(page)), \
             patch("fb_ads_scraper.scraper.LOAD_MORE_WAIT_SECONDS", 0), \
             patch("fb_ads_scraper.scraper.MAX_STAGNANT_SCROLLS", 3), \
             patch("fb_ads_scraper.scraper.parse_ad", side_effect=lambda ad: ad):
            self.assertEqual(len(scraper.run()), 1)
        self.assertEqual(button.clicks, 2)
        self.assertFalse(scraper.complete)
        self.assertEqual(scraper.stop_reason, "PAGINATION_STALLED")

    def test_stable_end_without_button_is_complete_when_total_and_ads_agree(self):
        page = _Page()
        page.default_body = "1 resultado"
        scraper = AdLibraryScraper("https://www.facebook.com/ads/library/")
        scraper.raw_ads = {"ad-1": {"ad_archive_id": "ad-1"}}
        scraper.raw_ads_observed = 1
        with patch("fb_ads_scraper.scraper.sync_playwright", return_value=_PlaywrightContext(page)), \
             patch("fb_ads_scraper.scraper.MAX_STAGNANT_SCROLLS", 2), \
             patch("fb_ads_scraper.scraper.parse_ad", side_effect=lambda ad: ad):
            scraper.run()
        self.assertTrue(scraper.complete)
        self.assertIsNone(scraper.stop_reason)

    def test_positive_total_with_only_initial_lot_marks_stagnant_pagination_incomplete(self):
        page = _Page()
        page.default_body = "~14000 resultados"
        scraper = AdLibraryScraper("https://www.facebook.com/ads/library/")
        scraper.raw_ads = {"ad-1": {"ad_archive_id": "ad-1"}}
        scraper.raw_ads_observed = 1
        with patch("fb_ads_scraper.scraper.sync_playwright", return_value=_PlaywrightContext(page)), \
             patch("fb_ads_scraper.scraper.MAX_STAGNANT_SCROLLS", 2), \
             patch("fb_ads_scraper.scraper.parse_ad", side_effect=lambda ad: ad):
            scraper.run()
        self.assertFalse(scraper.complete)
        self.assertEqual(scraper.stop_reason, "PAGINATION_STALLED")

    def test_profile_lock_rejects_a_second_process(self):
        import os
        import subprocess
        import sys
        from fb_ads_scraper.scraper import _exclusive_profile
        with tempfile.TemporaryDirectory() as directory, _exclusive_profile(directory):
            command = [sys.executable, "-c", (
                "from fb_ads_scraper.scraper import _exclusive_profile\n"
                f"with _exclusive_profile({directory!r}):\n    pass\n"
            )]
            result = subprocess.run(command, capture_output=True, text=True, check=False,
                                    env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SONDA_PROFILE_IN_USE", result.stderr)

    def test_total_only_rejects_mining_browser_options(self):
        with self.assertRaises(SystemExit) as error:
            main(["https://www.facebook.com/ads/library/", "--total-only",
                  "--profile-dir", "profile"])
        self.assertEqual(error.exception.code, 2)

    def test_total_only_rejects_cdp_endpoint(self):
        with self.assertRaises(SystemExit) as error:
            main(["https://www.facebook.com/ads/library/", "--total-only",
                  "--cdp-endpoint", "http://127.0.0.1:9222"])
        self.assertEqual(error.exception.code, 2)

    def test_cdp_endpoint_must_be_ipv4_loopback(self):
        with self.assertRaises(SystemExit) as error:
            main(["https://www.facebook.com/ads/library/", "--cdp-endpoint",
                  "http://0.0.0.0:9222"])
        self.assertEqual(error.exception.code, 2)

    def test_default_user_agent_override_remains_available_for_monitoring(self):
        page = _Page()
        context = _PlaywrightContext(page)
        scraper = AdLibraryScraper("https://www.facebook.com/ads/library/", total_only=True)
        with patch("fb_ads_scraper.scraper.sync_playwright", return_value=context):
            scraper.run()
        self.assertEqual(context.browser.context_options[0]["user_agent"],
                         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

    def test_total_only_polls_until_counter_appears(self):
        page = _Page()
        page.reads = ["Carregando", "", "15 resultados"]
        scraper = AdLibraryScraper("https://www.facebook.com/ads/library/", total_only=True)
        with patch("fb_ads_scraper.scraper.sync_playwright", return_value=_PlaywrightContext(page)):
            self.assertEqual(scraper.run(), [])
        self.assertEqual(scraper.total_results, 15)
        self.assertEqual(page.scrolls, 0)

    def test_total_only_empty_state_returns_official_zero_without_polling_deadline(self):
        page = _Page()
        page.reads = ["Nenhum anúncio corresponde aos seus critérios de pesquisa"]
        scraper = AdLibraryScraper("https://www.facebook.com/ads/library/", total_only=True)
        context = _PlaywrightContext(page)
        with patch("fb_ads_scraper.scraper.sync_playwright", return_value=context), \
             patch("fb_ads_scraper.scraper.time.monotonic", side_effect=[0, 0, 0]):
            self.assertEqual(scraper.run(), [])
        self.assertEqual(scraper.total_results, 0)
        self.assertEqual(scraper.total_results_source, "META_EMPTY_STATE")
        self.assertEqual(page.scrolls, 0)
        self.assertEqual(scraper.collection_duration_seconds, 0)
        self.assertTrue(context.browser.closed)

    def test_full_mining_empty_state_exits_before_any_scroll(self):
        page = _Page()
        page.reads = ["Nenhum anúncio corresponde aos seus critérios de pesquisa"]
        scraper = AdLibraryScraper("https://www.facebook.com/ads/library/")
        context = _PlaywrightContext(page)
        with patch("fb_ads_scraper.scraper.sync_playwright", return_value=context):
            self.assertEqual(scraper.run(), [])
        self.assertTrue(scraper.complete)
        self.assertTrue(scraper.empty_state_detected)
        self.assertEqual(scraper.total_results, 0)
        self.assertEqual(scraper.total_results_source, "META_EMPTY_STATE")
        self.assertEqual(page.scrolls, 0)
        self.assertTrue(context.browser.closed)

    def test_cli_exports_empty_monitoring_envelope_successfully(self):
        page = _Page()
        page.reads = ["Nenhum anúncio corresponde aos seus critérios de pesquisa"]
        with tempfile.TemporaryDirectory() as output, \
             patch("fb_ads_scraper.scraper.sync_playwright", return_value=_PlaywrightContext(page)):
            self.assertEqual(main([
                "https://www.facebook.com/ads/library/", "--total-only", "--result-envelope",
                "--format", "json", "--output", output,
            ]), 0)
            envelope = json.loads((Path(output) / "radar-result.json").read_text(encoding="utf-8"))
        self.assertEqual(envelope["totalResults"], 0)
        self.assertEqual(envelope["totalResultsSource"], "META_EMPTY_STATE")
        self.assertEqual(envelope["ads"], [])
        self.assertTrue(envelope["complete"])

    def test_cli_exports_mining_empty_state_successfully_without_allow_empty(self):
        page = _Page()
        page.reads = ["Nenhum anúncio corresponde aos seus critérios de pesquisa"]
        with tempfile.TemporaryDirectory() as output, \
             patch("fb_ads_scraper.scraper.sync_playwright", return_value=_PlaywrightContext(page)):
            self.assertEqual(main([
                "https://www.facebook.com/ads/library/", "--result-envelope",
                "--format", "json", "--output", output,
            ]), 0)
            envelope = json.loads((Path(output) / "radar-result.json").read_text(encoding="utf-8"))
        self.assertEqual(envelope["totalResults"], 0)
        self.assertEqual(envelope["totalResultsSource"], "META_EMPTY_STATE")
        self.assertEqual(envelope["ads"], [])
        self.assertTrue(envelope["complete"])

    def test_cli_does_not_call_positive_total_with_zero_cards_empty(self):
        scraper = SimpleNamespace(
            complete=True, stop_reason=None, collection_duration_seconds=1,
            raw_ads_observed=0, total_results=41000,
            total_results_source="META_RESULT_COUNTER", empty_state_detected=False,
            run=lambda: [],
        )
        with patch("fb_ads_scraper.cli.AdLibraryScraper", return_value=scraper):
            self.assertEqual(main([
                "https://www.facebook.com/ads/library/", "--format", "json", "--allow-empty",
            ]), 1)
        self.assertFalse(scraper.complete)
        self.assertEqual(scraper.stop_reason, "ADS_EXTRACTION_FAILED")

    def test_cli_does_not_accept_unknown_zero_as_empty(self):
        scraper = SimpleNamespace(
            complete=True, stop_reason=None, collection_duration_seconds=1,
            raw_ads_observed=0, total_results=0,
            total_results_source="META_RESULT_COUNTER", empty_state_detected=False,
            run=lambda: [],
        )
        with patch("fb_ads_scraper.cli.AdLibraryScraper", return_value=scraper):
            self.assertEqual(main(["https://www.facebook.com/ads/library/", "--allow-empty"]), 1)

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
