import unittest
import json
from datetime import datetime, timezone

from fb_ads_scraper.parser import parse_ad
from fb_ads_scraper.scraper import AdLibraryScraper


class ActorIdentityTests(unittest.TestCase):
    def test_parser_preserves_actor_identity_and_activity(self):
        ad = parse_ad({"ad_archive_id": "1", "page_id": "page", "actor_id": "actor", "is_active": True, "snapshot": {}})
        self.assertEqual((ad["page_id"], ad["actor_id"], ad["is_active"]), ("page", "actor", True))

    def test_ads_embedded_in_initial_application_json_are_extracted_and_deduplicated(self):
        first = {
            "ad_archive_id": "ad-1", "page_id": "page-1", "actor_id": "actor-1",
            "start_date": 1787000000, "is_active": True,
            "snapshot": {"link_url": "https://example.com/offer", "title": {"text": "Offer"}},
        }
        second = {
            "ad_archive_id": "ad-2", "page_id": "page-2", "actor_id": "actor-2",
            "start_date": 1787000100, "is_active": True,
            "snapshot": {"link_url": "https://example.com/other", "title": {"text": "Other"}},
        }
        # The live page embeds the same ad fields in nested application/json state.
        script = json.dumps({"data": {"ad_library_main": {"search_results_connection": {
            "edges": [{"node": first}, {"node": second}, {"node": first}],
        }}}})
        page = type("Page", (), {"eval_on_selector_all": lambda *_: [script]})()
        scraper = AdLibraryScraper("https://www.facebook.com/ads/library/")

        scraper._ingest_initial_html(page)
        ads = [parse_ad(raw) for raw in scraper.raw_ads.values()]

        self.assertEqual(scraper.raw_ads_observed, 3)
        self.assertEqual([ad["ad_archive_id"] for ad in ads], ["ad-1", "ad-2"])
        self.assertEqual(ads[0]["page_id"], "page-1")
        self.assertEqual(ads[0]["actor_id"], "actor-1")
        self.assertEqual(ads[0]["link_url"], "https://example.com/offer")
        self.assertEqual(ads[0]["start_date"], datetime.fromtimestamp(1787000000, tz=timezone.utc).strftime("%Y-%m-%d"))
        self.assertTrue(ads[0]["is_active"])
