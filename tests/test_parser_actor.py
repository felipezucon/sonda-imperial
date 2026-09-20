import unittest

from fb_ads_scraper.parser import parse_ad


class ActorIdentityTests(unittest.TestCase):
    def test_parser_preserves_actor_identity_and_activity(self):
        ad = parse_ad({"ad_archive_id": "1", "page_id": "page", "actor_id": "actor", "is_active": True, "snapshot": {}})
        self.assertEqual((ad["page_id"], ad["actor_id"], ad["is_active"]), ("page", "actor", True))
