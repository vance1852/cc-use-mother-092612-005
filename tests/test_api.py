import unittest
from datetime import datetime, timezone

from night_market_foundation.api import route
from night_market_foundation.clock import FixedClock
from night_market_foundation.service import DomainService
from night_market_foundation.signage import SignageService
from night_market_foundation.storage import Database

DIGEST_A = "a" * 64


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)

    def tearDown(self):
        self.database.close()

    def test_health_is_available_without_actor(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_route_returns_404(self):
        status, payload = route(self.service, "GET", "/missing", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_invalid_json_shape_returns_400(self):
        status, payload = route(self.service, "POST", "/organizations", {"request_id": "x"},
                                {"X-Actor-Id": "bootstrap"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])


class SignageApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = SignageService(
            self.database, FixedClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)))
        route(self.service, "POST", "/organizations",
              {"request_id": "org", "organization_id": "o1", "name": "活动机构"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "admin", "new_actor_id": "a1", "display_name": "管理员",
               "role": "admin", "organization_id": "o1"}, {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/sites",
              {"request_id": "site", "site_id": "s1", "organization_id": "o1",
               "name": "夜市一站", "timezone_name": "Asia/Shanghai"}, {"X-Actor-Id": "a1"})

    def tearDown(self):
        self.database.close()

    def test_sign_and_scan_flow_over_http(self):
        status, _ = route(self.service, "POST", "/signs",
                          {"request_id": "sign", "site_id": "s1", "sign_id": "sign1",
                           "label": "黄芪展项二维码牌"}, {"X-Actor-Id": "a1"})
        self.assertEqual(201, status)
        status, _ = route(self.service, "POST", "/deployments",
                          {"request_id": "deploy", "sign_id": "sign1", "zone_code": "zone-herb",
                           "exhibit_code": "exhibit-huangqi", "content_digest": DIGEST_A,
                           "effective_from": "2026-09-26T00:00:00Z"}, {"X-Actor-Id": "a1"})
        self.assertEqual(201, status)
        status, placement = route(self.service, "GET", "/signs/placement?sign_id=sign1", None)
        self.assertEqual(200, status)
        self.assertEqual("deployed", placement["status"])
        status, batch = route(self.service, "POST", "/scan-batches",
                              {"request_id": "batch", "site_id": "s1", "device_id": "terminal-1",
                               "uploads": [{"device_sequence": 1, "sign_code": "sign1",
                                            "content_digest": DIGEST_A,
                                            "occurred_at": "2026-09-26T07:30:00Z"}]},
                              {"X-Actor-Id": "a1"})
        self.assertEqual(201, status)
        self.assertEqual("valid", batch["results"][0]["classification"])
        status, stats = route(self.service, "GET", "/scan-statistics?site_id=s1", None)
        self.assertEqual(200, status)
        self.assertEqual(1, stats["totals"]["valid"])

    def test_inspection_claim_and_resolve_over_http(self):
        route(self.service, "POST", "/scan-batches",
              {"request_id": "batch", "site_id": "s1", "device_id": "terminal-1",
               "uploads": [{"device_sequence": 1, "sign_code": "ghost",
                            "content_digest": DIGEST_A,
                            "occurred_at": "2026-09-26T07:30:00Z"}]},
              {"X-Actor-Id": "a1"})
        status, payload = route(self.service, "GET",
                                "/inspections?site_id=s1&queue_type=unknown_sign", None)
        self.assertEqual(200, status)
        inspection_id = payload["items"][0]["inspection_id"]
        status, _ = route(self.service, "POST", "/inspections/claim",
                          {"request_id": "claim", "inspection_id": inspection_id},
                          {"X-Actor-Id": "a1"})
        self.assertEqual(201, status)
        status, detail = route(self.service, "GET", f"/inspections/{inspection_id}", None)
        self.assertEqual(200, status)
        self.assertEqual("claimed", detail["status"])
        status, _ = route(self.service, "POST", "/inspections/resolve",
                          {"request_id": "resolve", "inspection_id": inspection_id,
                           "evidence_type": "re_posting", "evidence_ref": "ref-1"},
                          {"X-Actor-Id": "a1"})
        self.assertEqual(201, status)

    def test_missing_query_param_returns_400(self):
        status, payload = route(self.service, "GET", "/signs/placement", None)
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_missing_inspection_returns_404(self):
        status, payload = route(self.service, "GET", "/inspections/nope", None)
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
