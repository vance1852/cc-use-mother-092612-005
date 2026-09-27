import unittest

from night_market_foundation.api import route
from night_market_foundation.service import DomainService
from night_market_foundation.storage import Database

D_A = "a" * 64
D_B = "b" * 64
D_C = "c" * 64


class PlaqueApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, actor="a1"):
        return route(self.service, method, path, body or {}, {"X-Actor-Id": actor})

    def bootstrap(self):
        self.call("POST", "/organizations", {
            "request_id": "org", "organization_id": "o1", "name": "夜市"}, actor="bootstrap")
        self.call("POST", "/actors", {
            "request_id": "admin", "new_actor_id": "a1", "display_name": "管理员",
            "role": "admin", "organization_id": "o1"}, actor="bootstrap")
        self.call("POST", "/actors", {
            "request_id": "op", "new_actor_id": "op1", "display_name": "操作员",
            "role": "operator", "organization_id": "o1"}, actor="a1")
        self.call("POST", "/sites", {
            "request_id": "site", "site_id": "s1", "organization_id": "o1",
            "name": "站点", "timezone_name": "Asia/Shanghai"})
        for request_id, category, key in (
                ("z1", "zone_registry", "z1"), ("z2", "zone_registry", "z2"),
                ("e1", "activity_resource", "e1"), ("e2", "activity_resource", "e2")):
            self.call("POST", "/domain-records", {
                "request_id": request_id, "site_id": "s1", "category": category,
                "external_key": key, "data": {"name": key}})

    def test_full_plaque_lifecycle_over_http(self):
        self.bootstrap()

        status, body = self.call("POST", "/plaques", {
            "request_id": "plaque-1", "site_id": "s1", "plaque_id": "Q1"}, actor="op1")
        self.assertEqual(201, status)
        status, replay = self.call("POST", "/plaques", {
            "request_id": "plaque-1", "site_id": "s1", "plaque_id": "Q1"}, actor="op1")
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])

        status, deploy = self.call("POST", "/plaques/deploy", {
            "request_id": "deploy-1", "plaque_id": "Q1", "zone_id": "z1", "exhibit_id": "e1",
            "content_summary": "甘草简介", "content_digest": D_A}, actor="op1")
        self.assertEqual(201, status)

        status, upload = self.call("POST", "/scans/upload", {
            "device_serial": "term-001", "site_id": "s1", "request_id": "up-1",
            "events": [
                {"plaque_code": "Q1", "event_seq": 0, "scanned_at": "2026-09-27T01:00:00Z",
                 "zone_id": "z1", "exhibit_id": "e1", "content_digest": D_A},
                {"plaque_code": "ZZ", "event_seq": 1, "scanned_at": "2026-09-27T01:01:00Z"},
            ]})
        self.assertEqual(202, status)
        self.assertEqual("ok", upload["results"][0]["verdict"])
        self.assertEqual("unknown_plaque", upload["results"][1]["verdict"])
        unknown_case = upload["results"][1]["case_id"]

        # 同序号同内容 = 重放；同序号不同内容 = 分叉
        status, replay_batch = self.call("POST", "/scans/upload", {
            "device_serial": "term-001", "site_id": "s1", "request_id": "up-1",
            "events": [
                {"plaque_code": "Q1", "event_seq": 0, "scanned_at": "2026-09-27T01:00:00Z",
                 "zone_id": "z1", "exhibit_id": "e1", "content_digest": D_A},
                {"plaque_code": "ZZ", "event_seq": 1, "scanned_at": "2026-09-27T01:01:00Z"},
            ]})
        self.assertEqual(202, status)
        self.assertEqual(2, replay_batch["accepted"])

        status, fork = self.call("POST", "/scans/upload", {
            "device_serial": "term-001", "site_id": "s1",
            "events": [{"plaque_code": "Q1", "event_seq": 0,
                        "scanned_at": "2026-09-27T01:00:00Z",
                        "zone_id": "z1", "exhibit_id": "e1", "content_digest": D_B}]})
        self.assertEqual(1, fork["forked"])

        # 交接换位：先释放再签收，旧位置立即失效
        status, rel = self.call("POST", "/relocations", {
            "request_id": "rel-1", "plaque_id": "Q1", "zone_id": "z2", "exhibit_id": "e2",
            "content_summary": "黄芪简介", "content_digest": D_C}, actor="op1")
        self.assertEqual(201, status)
        status, _ = self.call("POST", "/relocations/handover", {
            "relocation_id": rel["relocation_id"], "action": "release"}, actor="op1")
        self.assertEqual(200, status)
        status, done = self.call("POST", "/relocations/handover", {
            "relocation_id": rel["relocation_id"], "action": "receive"}, actor="a1")
        self.assertEqual(200, status)
        self.assertEqual("completed", done["status"])

        status, stale = self.call("POST", "/scans/upload", {
            "device_serial": "term-002", "site_id": "s1",
            "events": [{"plaque_code": "Q1", "event_seq": 0,
                        "scanned_at": "2026-09-27T02:00:00Z", "zone_id": "z1"}]})
        stale_case = stale["results"][0]["case_id"]
        self.assertEqual("stale_position", stale["results"][0]["verdict"])

        status, position = self.call("GET", "/plaques/position?plaque_id=Q1", None)
        self.assertEqual(200, status)
        self.assertEqual("z2", position["zone_id"])
        self.assertEqual("e2", position["exhibit_id"])
        self.assertTrue(position["relocated"])
        self.assertIsNotNone(position["anomaly_started_at"])

        status, queue = self.call("GET", "/inspection-queue?site_id=s1", None)
        self.assertEqual(200, status)
        kinds = {item["kind"] for item in queue["items"]}
        self.assertEqual({"unknown_plaque", "stale_position"}, kinds)

        status, claimed = self.call("POST", "/inspection-cases/claim", {
            "case_id": stale_case}, actor="op1")
        self.assertEqual(200, status)
        self.assertIn("claim_expires_at", claimed)

        status, resolved = self.call("POST", "/inspection-cases/resolve", {
            "case_id": stale_case, "evidence_type": "repost",
            "evidence_ref": "photo-2026-09-27-01"}, actor="op1")
        self.assertEqual(200, status)
        self.assertTrue(resolved["protected"])

        status, stats = self.call("GET", "/statistics/anonymous?site_id=s1", None)
        self.assertEqual(200, status)
        self.assertFalse(stats["policy"]["raw_device_serial_stored"])
        self.assertEqual("per_day_salted_device_pseudonym", stats["policy"]["identifier"])
        self.assertEqual(1, stats["open_inspection_cases"]["unknown_plaque"])

        status, supplements = self.call(
            "GET", f"/inspection-cases/supplements?case_id={unknown_case}", None)
        self.assertEqual(200, status)
        self.assertEqual([], supplements["items"])

    def test_unknown_plaque_route_needs_site(self):
        status, body = self.call("GET", "/inspection-queue", None)
        self.assertEqual(400, status)


if __name__ == "__main__":
    unittest.main()
