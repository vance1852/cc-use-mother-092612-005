import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from night_market_foundation.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from night_market_foundation.signage import SignageService
from night_market_foundation.storage import Database

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
START = datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)


class ManualClock:
    """可手动推进的测试时钟。"""

    def __init__(self, value):
        self._value = value

    def now(self):
        return self._value

    def advance(self, **kwargs):
        self._value += timedelta(**kwargs)


class SignageTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = ManualClock(START)
        self.service = SignageService(self.database, self.clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="活动机构一")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="op1", actor_id="a1", new_actor_id="op1",
                                    display_name="操作员一", role="operator", organization_id="o1")
        self.service.register_actor(request_id="op2", actor_id="a1", new_actor_id="op2",
                                    display_name="操作员二", role="operator", organization_id="o1")
        self.service.register_actor(request_id="rev1", actor_id="a1", new_actor_id="rev1",
                                    display_name="巡检员", role="reviewer", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="op1", site_id="s1",
                                   organization_id="o1", name="夜市一站", timezone_name="Asia/Shanghai")
        self.service.register_sign(request_id="sign", actor_id="op1", site_id="s1",
                                   sign_id="sign1", label="黄芪展项二维码牌")
        self.service.deploy_sign(request_id="deploy", actor_id="op1", sign_id="sign1",
                                 zone_code="zone-herb", exhibit_code="exhibit-huangqi",
                                 content_digest=DIGEST_A, effective_from="2026-09-26T00:00:00Z")

    def tearDown(self):
        self.database.close()

    def _upload(self, uploads, request_id="batch-1", device_id="terminal-1"):
        return self.service.upload_scans(request_id=request_id, actor_id="op1", site_id="s1",
                                         device_id=device_id, uploads=uploads)

    def _scan(self, sequence, sign_code="sign1", content_digest=DIGEST_A,
              occurred_at="2026-09-26T07:30:00Z"):
        return {"device_sequence": sequence, "sign_code": sign_code,
                "content_digest": content_digest, "occurred_at": occurred_at}

    # ------------------------------------------------------------------
    # 标牌身份与布设窗口
    # ------------------------------------------------------------------

    def test_sign_identity_is_stable_across_deployments(self):
        self.service.begin_transfer(request_id="t1", actor_id="op1", sign_id="sign1",
                                    zone_code="zone-tea", exhibit_code="exhibit-tea",
                                    content_digest=DIGEST_B)
        self.service.confirm_transfer(request_id="t1-confirm", actor_id="op2", transfer_id=self._pending_transfer())
        placement = self.service.sign_placement("sign1")
        self.assertEqual("sign1", placement["sign_id"])
        self.assertEqual("zone-tea", placement["zone_code"])

    def _pending_transfer(self):
        row = self.database.connection.execute(
            "SELECT transfer_id FROM sign_transfers WHERE status='pending'").fetchone()
        return row["transfer_id"]

    def test_duplicate_sign_rejected(self):
        with self.assertRaises(ConflictError):
            self.service.register_sign(request_id="sign-dup", actor_id="op1", site_id="s1",
                                       sign_id="sign1", label="重复标牌")

    def test_second_active_deployment_rejected(self):
        with self.assertRaises(ConflictError):
            self.service.deploy_sign(request_id="deploy-2", actor_id="op1", sign_id="sign1",
                                     zone_code="zone-tea", exhibit_code="exhibit-tea",
                                     content_digest=DIGEST_B,
                                     effective_from="2026-09-26T01:00:00Z")

    def test_deployment_window_must_be_ordered(self):
        with self.assertRaises(ValidationError):
            self.service.deploy_sign(request_id="deploy-bad", actor_id="op1", sign_id="sign1",
                                     zone_code="zone-tea", exhibit_code="exhibit-tea",
                                     content_digest=DIGEST_B,
                                     effective_from="2026-09-26T02:00:00Z",
                                     effective_to="2026-09-26T01:00:00Z")

    def test_redeploy_after_window_expires(self):
        self.service.register_sign(request_id="sign2", actor_id="op1", site_id="s1",
                                   sign_id="sign2", label="限时展项牌")
        self.service.deploy_sign(request_id="deploy-s2", actor_id="op1", sign_id="sign2",
                                 zone_code="zone-herb", exhibit_code="exhibit-flash",
                                 content_digest=DIGEST_A,
                                 effective_from="2026-09-26T00:00:00Z",
                                 effective_to="2026-09-26T06:00:00Z")
        self.service.deploy_sign(request_id="deploy-s2b", actor_id="op1", sign_id="sign2",
                                 zone_code="zone-tea", exhibit_code="exhibit-tea",
                                 content_digest=DIGEST_B,
                                 effective_from="2026-09-26T06:00:00Z")
        placement = self.service.sign_placement("sign2")
        self.assertEqual("zone-tea", placement["zone_code"])

    # ------------------------------------------------------------------
    # 回传分拣：重放、分叉与三类异常队列
    # ------------------------------------------------------------------

    def test_valid_scan_accepted_and_counted(self):
        result = self._upload([self._scan(1)])
        self.assertEqual("accepted", result["results"][0]["status"])
        self.assertEqual("valid", result["results"][0]["classification"])
        stats = self.service.scan_statistics("s1")
        self.assertEqual(1, stats["totals"]["valid"])
        self.assertEqual([{"zone_code": "zone-herb", "exhibit_code": "exhibit-huangqi", "valid": 1}],
                         stats["per_zone"])

    def test_same_device_reupload_is_replay(self):
        self._upload([self._scan(1)], request_id="batch-1")
        again = self._upload([self._scan(1)], request_id="batch-2")
        self.assertEqual("replayed", again["results"][0]["status"])
        stats = self.service.scan_statistics("s1")
        self.assertEqual(1, stats["totals"]["valid"])
        self.assertEqual(1, stats["devices"][0]["replayed"])

    def test_same_sequence_with_different_content_is_fork(self):
        self._upload([self._scan(1)], request_id="batch-1")
        forked = self._upload([self._scan(1, content_digest=DIGEST_B)], request_id="batch-2")
        self.assertEqual("fork", forked["results"][0]["status"])
        stats = self.service.scan_statistics("s1")
        self.assertEqual(1, stats["totals"]["valid"])
        self.assertEqual(1, stats["devices"][0]["forked"])

    def test_same_request_id_replays_whole_batch(self):
        first = self._upload([self._scan(1)], request_id="batch-1")
        second = self._upload([self._scan(1)], request_id="batch-1")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["results"], second["results"])
        self.assertEqual(1, self.service.scan_statistics("s1")["totals"]["valid"])

    def test_unknown_sign_goes_to_its_queue(self):
        result = self._upload([self._scan(1, sign_code="ghost-sign")])
        item = result["results"][0]
        self.assertEqual("unknown_sign", item["classification"])
        inspections = self.service.list_inspections("s1", queue_type="unknown_sign")
        self.assertEqual(1, len(inspections))
        self.assertEqual("ghost-sign", inspections[0]["sign_code"])
        self.assertIsNone(inspections[0]["sign_id"])

    def test_scan_outside_window_is_invalid_position(self):
        result = self._upload([self._scan(1, occurred_at="2026-09-25T23:00:00Z")])
        self.assertEqual("invalid_position", result["results"][0]["classification"])
        inspections = self.service.list_inspections("s1", queue_type="invalid_position")
        self.assertEqual(1, len(inspections))

    def test_digest_mismatch_goes_to_its_queue(self):
        result = self._upload([self._scan(1, content_digest=DIGEST_B)])
        self.assertEqual("digest_mismatch", result["results"][0]["classification"])
        inspections = self.service.list_inspections("s1", queue_type="digest_mismatch")
        self.assertEqual(1, len(inspections))
        self.assertEqual("sign1", inspections[0]["sign_id"])

    def test_repeated_anomalies_share_one_open_inspection(self):
        self._upload([self._scan(1, content_digest=DIGEST_B)], request_id="b1")
        self._upload([self._scan(2, content_digest=DIGEST_B)], request_id="b2")
        inspections = self.service.list_inspections("s1", queue_type="digest_mismatch")
        self.assertEqual(1, len(inspections))
        self.assertEqual(2, inspections[0]["event_count"])

    def test_malformed_item_rejected_without_blocking_batch(self):
        result = self._upload([self._scan(1), {"device_sequence": "x", "sign_code": "sign1",
                                             "content_digest": DIGEST_A,
                                             "occurred_at": "2026-09-26T07:30:00Z"}])
        self.assertEqual("accepted", result["results"][0]["status"])
        self.assertEqual("rejected", result["results"][1]["status"])
        self.assertEqual(1, result["summary"]["accepted"])
        self.assertEqual(1, result["summary"]["rejected"])

    def test_scan_events_hold_no_personal_identifier(self):
        self._upload([{**self._scan(1), "visitor_id": "person-123", "phone": "13800000000"}])
        row = self.database.connection.execute("SELECT * FROM scan_events").fetchone()
        self.assertNotIn("person-123", str(dict(row)))
        self.assertNotIn("13800000000", str(dict(row)))

    # ------------------------------------------------------------------
    # 换位交接
    # ------------------------------------------------------------------

    def test_transfer_invalidates_old_position_immediately(self):
        self.service.begin_transfer(request_id="t1", actor_id="op1", sign_id="sign1",
                                    zone_code="zone-tea", exhibit_code="exhibit-tea",
                                    content_digest=DIGEST_B)
        placement = self.service.sign_placement("sign1")
        self.assertEqual("in_transit", placement["status"])
        # 旧位置立即失效：交接完成前扫描按失效位置入队
        result = self._upload([self._scan(1, occurred_at="2026-09-26T08:30:00Z")])
        self.assertEqual("invalid_position", result["results"][0]["classification"])

    def test_transfer_requires_two_distinct_parties(self):
        self.service.begin_transfer(request_id="t1", actor_id="op1", sign_id="sign1",
                                    zone_code="zone-tea", exhibit_code="exhibit-tea",
                                    content_digest=DIGEST_B)
        transfer_id = self._pending_transfer()
        with self.assertRaises(ConflictError):
            self.service.confirm_transfer(request_id="t1-self", actor_id="op1",
                                          transfer_id=transfer_id)
        self.service.confirm_transfer(request_id="t1-ok", actor_id="op2",
                                      transfer_id=transfer_id)
        placement = self.service.sign_placement("sign1")
        self.assertEqual("deployed", placement["status"])
        self.assertEqual("zone-tea", placement["zone_code"])

    def test_second_transfer_rejected_while_pending(self):
        self.service.begin_transfer(request_id="t1", actor_id="op1", sign_id="sign1",
                                    zone_code="zone-tea", exhibit_code="exhibit-tea",
                                    content_digest=DIGEST_B)
        with self.assertRaises(ConflictError):
            self.service.begin_transfer(request_id="t2", actor_id="op1", sign_id="sign1",
                                        zone_code="zone-pantry", exhibit_code="exhibit-pantry",
                                        content_digest=DIGEST_C)

    def test_history_scans_stay_with_old_deployment(self):
        self._upload([self._scan(1, occurred_at="2026-09-26T07:30:00Z")], request_id="b1")
        self.service.begin_transfer(request_id="t1", actor_id="op1", sign_id="sign1",
                                    zone_code="zone-tea", exhibit_code="exhibit-tea",
                                    content_digest=DIGEST_B)
        self.service.confirm_transfer(request_id="t1-ok", actor_id="op2",
                                      transfer_id=self._pending_transfer())
        # 换位前的历史扫描仍归属原专区，不迁移
        stats = self.service.scan_statistics("s1")
        self.assertEqual([{"zone_code": "zone-herb", "exhibit_code": "exhibit-huangqi", "valid": 1}],
                         stats["per_zone"])
        row = self.database.connection.execute(
            "SELECT d.zone_code FROM scan_events e "
            "JOIN sign_deployments d ON d.deployment_id=e.deployment_id").fetchone()
        self.assertEqual("zone-herb", row["zone_code"])

    # ------------------------------------------------------------------
    # 巡检任务：领取、过期、结案证据
    # ------------------------------------------------------------------

    def _open_mismatch_inspection(self):
        self._upload([self._scan(1, content_digest=DIGEST_B)], request_id="anomaly-batch")
        return self.service.list_inspections("s1", queue_type="digest_mismatch")[0]["inspection_id"]

    def test_claim_sets_explicit_expiry(self):
        inspection_id = self._open_mismatch_inspection()
        receipt = self.service.claim_inspection(request_id="c1", actor_id="rev1",
                                                inspection_id=inspection_id, ttl_minutes=60)
        self.assertFalse(receipt.replayed)
        item = self.service.get_inspection(inspection_id)
        self.assertEqual("claimed", item["status"])
        expected = (START + timedelta(minutes=60)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        self.assertEqual(expected, item["claim_expires_at"])

    def test_second_claim_blocked_until_expiry(self):
        inspection_id = self._open_mismatch_inspection()
        self.service.claim_inspection(request_id="c1", actor_id="rev1",
                                      inspection_id=inspection_id, ttl_minutes=30)
        with self.assertRaises(ConflictError):
            self.service.claim_inspection(request_id="c2", actor_id="op1",
                                          inspection_id=inspection_id)
        self.clock.advance(minutes=31)
        self.service.claim_inspection(request_id="c3", actor_id="op1",
                                      inspection_id=inspection_id)
        self.assertEqual("op1", self.service.get_inspection(inspection_id)["claimed_by"])

    def test_resolve_requires_claimer_and_valid_evidence(self):
        inspection_id = self._open_mismatch_inspection()
        with self.assertRaises(ConflictError):
            self.service.resolve_inspection(request_id="r0", actor_id="rev1",
                                            inspection_id=inspection_id,
                                            evidence_type="re_posting", evidence_ref="ref-1")
        self.service.claim_inspection(request_id="c1", actor_id="rev1",
                                      inspection_id=inspection_id)
        with self.assertRaises(PermissionDenied):
            self.service.resolve_inspection(request_id="r1", actor_id="op1",
                                            inspection_id=inspection_id,
                                            evidence_type="re_posting", evidence_ref="ref-1")
        with self.assertRaises(ValidationError):
            self.service.resolve_inspection(request_id="r2", actor_id="rev1",
                                            inspection_id=inspection_id,
                                            evidence_type="verbal_ok", evidence_ref="ref-1")
        self.service.resolve_inspection(request_id="r3", actor_id="rev1",
                                        inspection_id=inspection_id,
                                        evidence_type="on_site_replacement", evidence_ref="ref-1")
        item = self.service.get_inspection(inspection_id)
        self.assertEqual("resolved", item["status"])
        self.assertEqual("on_site_replacement", item["evidence_type"])
        with self.assertRaises(ConflictError):
            self.service.resolve_inspection(request_id="r4", actor_id="rev1",
                                            inspection_id=inspection_id,
                                            evidence_type="re_posting", evidence_ref="ref-2")

    def test_resolve_after_claim_expiry_rejected(self):
        inspection_id = self._open_mismatch_inspection()
        self.service.claim_inspection(request_id="c1", actor_id="rev1",
                                      inspection_id=inspection_id, ttl_minutes=30)
        self.clock.advance(minutes=31)
        with self.assertRaises(ConflictError):
            self.service.resolve_inspection(request_id="r1", actor_id="rev1",
                                            inspection_id=inspection_id,
                                            evidence_type="re_posting", evidence_ref="ref-1")

    def test_late_event_supplements_without_overturning_conclusion(self):
        inspection_id = self._open_mismatch_inspection()
        self.service.claim_inspection(request_id="c1", actor_id="rev1",
                                      inspection_id=inspection_id)
        self.service.resolve_inspection(request_id="r1", actor_id="rev1",
                                        inspection_id=inspection_id,
                                        evidence_type="false_report_review", evidence_ref="ref-1")
        resolved_at = self.service.get_inspection(inspection_id)["resolved_at"]
        # 迟到事件（发生在结案之前）：仅作为补充挂上，结论保持 resolved
        late = self._upload([self._scan(2, content_digest=DIGEST_B,
                                      occurred_at="2026-09-26T07:35:00Z")], request_id="late-batch")
        self.assertTrue(late["results"][0]["supplement"])
        item = self.service.get_inspection(inspection_id)
        self.assertEqual("resolved", item["status"])
        self.assertEqual(resolved_at, item["resolved_at"])
        self.assertEqual(1, item["supplement_count"])
        self.assertEqual(2, item["event_count"])
        # 结案之后新发生的异常：开启新的巡检任务
        fresh = self._upload([self._scan(3, content_digest=DIGEST_B,
                                       occurred_at="2026-09-26T09:00:00Z")], request_id="fresh-batch")
        self.assertNotIn("supplement", fresh["results"][0])
        self.assertNotEqual(inspection_id, fresh["results"][0]["inspection_id"])
        opened = self.service.list_inspections("s1", status="open", queue_type="digest_mismatch")
        self.assertEqual(1, len(opened))

    # ------------------------------------------------------------------
    # 运营查询与重启持久化
    # ------------------------------------------------------------------

    def test_anomaly_origin_points_to_earliest_event(self):
        self._upload([self._scan(1, content_digest=DIGEST_B,
                               occurred_at="2026-09-26T07:40:00Z")], request_id="b1")
        self._upload([self._scan(2, sign_code="ghost",
                               occurred_at="2026-09-26T07:35:00Z")], request_id="b2")
        self._upload([self._scan(3, content_digest=DIGEST_B,
                               occurred_at="2026-09-26T07:35:00Z")], request_id="b3")
        origin = self.service.sign_anomaly_origin("sign1")["origin"]
        self.assertEqual("digest_mismatch", origin["classification"])
        self.assertEqual("2026-09-26T07:35:00.000000Z", origin["occurred_at"])
        self.assertIsNotNone(origin["inspection_id"])
        unknown = self.database.connection.execute(
            "SELECT sign_id FROM signs WHERE sign_id='ghost'").fetchone()
        self.assertIsNone(unknown)

    def test_placement_lifecycle(self):
        self.service.register_sign(request_id="sign3", actor_id="op1", site_id="s1",
                                   sign_id="sign3", label="待布设牌")
        self.assertEqual("undeployed", self.service.sign_placement("sign3")["status"])
        self.assertEqual("deployed", self.service.sign_placement("sign1")["status"])
        self.service.begin_transfer(request_id="t1", actor_id="op1", sign_id="sign1",
                                    zone_code="zone-tea", exhibit_code="exhibit-tea",
                                    content_digest=DIGEST_B)
        self.assertEqual("in_transit", self.service.sign_placement("sign1")["status"])

    def test_statistics_distinguish_damage_position_and_reupload(self):
        self._upload([
            self._scan(1),                                             # 有效
            self._scan(2, content_digest=DIGEST_B),                    # 摘要不符
            self._scan(3, sign_code="ghost"),                          # 未知标牌（牌面损坏线索）
            self._scan(4, occurred_at="2026-09-25T23:00:00Z"),         # 失效位置
        ], request_id="b1")
        self._upload([self._scan(1)], request_id="b2")                 # 重复补传
        stats = self.service.scan_statistics("s1")
        self.assertEqual({"valid": 1, "invalid_position": 1, "unknown_sign": 1,
                          "digest_mismatch": 1}, stats["totals"])
        self.assertEqual(1, stats["devices"][0]["replayed"])
        self.assertEqual(1, stats["inspections"]["digest_mismatch"]["open"])
        self.assertEqual(1, stats["inspections"]["unknown_sign"]["open"])
        self.assertEqual(1, stats["inspections"]["invalid_position"]["open"])

    def test_unclosed_inspection_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.sqlite3"
            database = Database(path)
            clock = ManualClock(START)
            service = SignageService(database, clock)
            service.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="o1", name="活动机构一")
            service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
            service.register_site(request_id="site", actor_id="a1", site_id="s1",
                                  organization_id="o1", name="夜市一站", timezone_name="Asia/Shanghai")
            service.register_sign(request_id="sign", actor_id="a1", site_id="s1",
                                  sign_id="sign1", label="黄芪展项二维码牌")
            service.upload_scans(request_id="b1", actor_id="a1", site_id="s1",
                                 device_id="terminal-1",
                                 uploads=[self._scan(1, sign_code="ghost")])
            inspection_id = service.list_inspections("s1")[0]["inspection_id"]
            service.claim_inspection(request_id="c1", actor_id="a1",
                                     inspection_id=inspection_id, ttl_minutes=90)
            database.close()

            # 应用重启：未结巡检保持 claimed 状态与过期时间
            database = Database(path)
            service = SignageService(database, clock)
            item = service.get_inspection(inspection_id)
            self.assertEqual("claimed", item["status"])
            self.assertEqual("a1", item["claimed_by"])
            expected = (START + timedelta(minutes=90)).isoformat(timespec="microseconds").replace("+00:00", "Z")
            self.assertEqual(expected, item["claim_expires_at"])
            service.resolve_inspection(request_id="r1", actor_id="a1",
                                       inspection_id=inspection_id,
                                       evidence_type="re_posting", evidence_ref="ref-9")
            self.assertEqual("resolved", service.get_inspection(inspection_id)["status"])
            database.close()

    def test_missing_objects_raise_not_found(self):
        with self.assertRaises(NotFoundError):
            self.service.sign_placement("missing")
        with self.assertRaises(NotFoundError):
            self.service.get_inspection("missing")
        with self.assertRaises(NotFoundError):
            self.service.confirm_transfer(request_id="tx", actor_id="op1", transfer_id="missing")


if __name__ == "__main__":
    unittest.main()
