import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from night_market_foundation.clock import FixedClock
from night_market_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from night_market_foundation.plaques import PlaqueService
from night_market_foundation.service import DomainService
from night_market_foundation.storage import Database

D_A = "a" * 64
D_B = "b" * 64
D_C = "c" * 64
T0 = datetime(2026, 9, 27, tzinfo=timezone.utc)


class PlaqueTestBase(unittest.TestCase):
    claim_ttl = 100

    def setUp(self):
        self.clock = FixedClock(T0)
        self.database = Database()
        self.service = DomainService(self.database, self.clock)
        self.plaques = PlaqueService(self.database, self.clock, claim_ttl_seconds=self.claim_ttl)
        self._bootstrap()

    def tearDown(self):
        self.database.close()

    def _bootstrap(self):
        s = self.service
        s.register_organization(request_id="org", actor_id="bootstrap", organization_id="o1", name="夜市")
        s.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                         display_name="管理员", role="admin", organization_id="o1")
        for key, name, role in (("op1", "移出方", "operator"), ("op2", "接收方", "operator"),
                                ("rv1", "复核", "reviewer"), ("au1", "审计", "auditor")):
            s.register_actor(request_id=f"actor-{key}", actor_id="a1", new_actor_id=key,
                             display_name=name, role=role, organization_id="o1")
        s.register_site(request_id="site", actor_id="a1", site_id="s1", organization_id="o1",
                        name="一号站点", timezone_name="Asia/Shanghai")
        s.record_domain_data(request_id="zone-1", actor_id="a1", site_id="s1",
                             category="zone_registry", external_key="z1", data={"name": "甘草专区"})
        s.record_domain_data(request_id="zone-2", actor_id="a1", site_id="s1",
                             category="zone_registry", external_key="z2", data={"name": "黄芪专区"})
        s.record_domain_data(request_id="exhibit-1", actor_id="a1", site_id="s1",
                             category="activity_resource", external_key="e1", data={"name": "甘草展项"})
        s.record_domain_data(request_id="exhibit-2", actor_id="a1", site_id="s1",
                             category="activity_resource", external_key="e2", data={"name": "黄芪展项"})

    def advance(self, seconds: int):
        new_clock = FixedClock(T0 + timedelta(seconds=seconds))
        self.clock = new_clock
        self.service.clock = new_clock
        self.plaques.clock = new_clock

    def deploy_q1(self, plaque_id="Q1", zone="z1", exhibit="e1", digest=D_A, request_id="dep"):
        self.plaques.register_plaque(request_id="reg", actor_id="op1", site_id="s1", plaque_id=plaque_id)
        return self.plaques.deploy_plaque(
            request_id=request_id, actor_id="op1", plaque_id=plaque_id, zone_id=zone,
            exhibit_id=exhibit, content_summary="展项摘要", content_digest=digest)

    def scan(self, *, serial="dev-1", seq=0, code="Q1", zone="z1", exhibit="e1",
             digest=D_A, at="2026-09-27T01:00:00Z"):
        event = {"plaque_code": code, "event_seq": seq, "scanned_at": at}
        if zone is not None:
            event["zone_id"] = zone
        if exhibit is not None:
            event["exhibit_id"] = exhibit
        if digest is not None:
            event["content_digest"] = digest
        return self.plaques.upload_scans(device_serial=serial, site_id="s1", events=[event])


class IdentityAndDeploymentTest(PlaqueTestBase):
    def test_identity_stable_and_history_not_migrated_after_relocation(self):
        self.deploy_q1()
        first_deployment = self.plaques.current_deployment("Q1").deployment_id
        before = self.scan(seq=0)
        rel = self.plaques.request_relocation(
            request_id="rel", actor_id="op1", plaque_id="Q1", zone_id="z2", exhibit_id="e2",
            content_summary="黄芪摘要", content_digest=D_C)
        self.plaques.handover_relocation(actor_id="op1", relocation_id=rel["relocation_id"], action="release")
        done = self.plaques.handover_relocation(actor_id="op2", relocation_id=rel["relocation_id"], action="receive")

        position = self.plaques.plaque_position("Q1")
        self.assertEqual("z2", position.zone_id)
        self.assertEqual("e2", position.exhibit_id)
        self.assertTrue(position.relocated)
        self.assertEqual(done["new_deployment_id"], self.plaques.current_deployment("Q1").deployment_id)
        self.assertNotEqual(first_deployment, done["new_deployment_id"])

        old = self.plaques.content_summary(first_deployment)
        self.assertEqual("z1", old["zone_id"])
        self.assertEqual(done["effective_from"], old["effective_to"])
        # 旧布设记录保留且已失效，新生效窗口是独立记录，历史扫描仍指向旧布设不迁移
        self.assertEqual(2, self.database.connection.execute(
            "SELECT COUNT(*) FROM deployments WHERE plaque_id='Q1'").fetchone()[0])
        self.assertEqual(1, self.database.connection.execute(
            "SELECT COUNT(*) FROM deployments WHERE plaque_id='Q1' AND effective_to IS NULL").fetchone()[0])
        self.assertEqual(first_deployment, self.database.connection.execute(
            "SELECT deployment_id FROM scan_events WHERE event_id=?",
            (before.results[0].event_id,)).fetchone()[0])

    def test_handover_requires_release_before_receive(self):
        self.deploy_q1()
        rel = self.plaques.request_relocation(
            request_id="rel", actor_id="op1", plaque_id="Q1", zone_id="z2", exhibit_id="e2",
            content_summary="新摘要", content_digest=D_C)
        with self.assertRaises(ConflictError):
            self.plaques.handover_relocation(actor_id="op2", relocation_id=rel["relocation_id"], action="receive")
        self.plaques.handover_relocation(actor_id="op1", relocation_id=rel["relocation_id"], action="release")
        with self.assertRaises(ConflictError):
            self.plaques.handover_relocation(actor_id="op1", relocation_id=rel["relocation_id"], action="receive")

    def test_old_position_stays_valid_until_handover_completed(self):
        self.deploy_q1()
        self.plaques.request_relocation(
            request_id="rel", actor_id="op1", plaque_id="Q1", zone_id="z2", exhibit_id="e2",
            content_summary="新摘要", content_digest=D_C)
        report = self.scan(seq=0)
        self.assertEqual("ok", report.results[0].verdict)

    def test_deploy_requires_registered_zone_and_exhibit(self):
        self.plaques.register_plaque(request_id="reg", actor_id="op1", site_id="s1", plaque_id="Q9")
        with self.assertRaises(NotFoundError):
            self.plaques.deploy_plaque(
                request_id="dep", actor_id="op1", plaque_id="Q9", zone_id="zz", exhibit_id="e1",
                content_summary="摘要", content_digest=D_A)

    def test_redeploy_must_use_handover(self):
        self.deploy_q1()
        with self.assertRaises(ConflictError):
            self.plaques.deploy_plaque(
                request_id="dep2", actor_id="op1", plaque_id="Q1", zone_id="z2", exhibit_id="e2",
                content_summary="摘要", content_digest=D_C)

    def test_write_idempotency_replays_and_rejects_changed_payload(self):
        first = self.plaques.register_plaque(request_id="reg", actor_id="op1", site_id="s1", plaque_id="Q1")
        second = self.plaques.register_plaque(request_id="reg", actor_id="op1", site_id="s1", plaque_id="Q1")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        with self.assertRaises(ConflictError):
            self.plaques.register_plaque(request_id="reg", actor_id="op1", site_id="s1", plaque_id="Q2")

    def test_reviewer_and_auditor_cannot_change_deployment(self):
        with self.assertRaises(PermissionDenied):
            self.plaques.register_plaque(request_id="x", actor_id="rv1", site_id="s1", plaque_id="Q1")
        with self.assertRaises(PermissionDenied):
            self.plaques.register_plaque(request_id="y", actor_id="au1", site_id="s1", plaque_id="Q2")


class ScanClassificationTest(PlaqueTestBase):
    def test_verdict_routes_to_separate_queues(self):
        self.deploy_q1()
        self.assertEqual("ok", self.scan(seq=0).results[0].verdict)

        stale = self.scan(serial="dev-2", seq=0, zone="z1", exhibit="e2", digest=D_A)
        self.assertEqual("stale_position", stale.results[0].verdict)
        unknown = self.scan(serial="dev-3", seq=0, code="ZZ", zone=None, exhibit=None, digest=None)
        self.assertEqual("unknown_plaque", unknown.results[0].verdict)
        mismatch = self.scan(serial="dev-4", seq=0, zone="z1", exhibit="e1", digest=D_B)
        self.assertEqual("digest_mismatch", mismatch.results[0].verdict)

        queue = self.plaques.list_inspection_queue(site_id="s1")
        kinds = {case.kind: case for case in queue}
        self.assertEqual({"stale_position", "unknown_plaque", "digest_mismatch"}, set(kinds))
        self.assertIsNone(kinds["unknown_plaque"].plaque_id)
        self.assertEqual("Q1", kinds["stale_position"].plaque_id)

    def test_repeated_anomaly_supplements_same_case(self):
        self.deploy_q1()
        first = self.scan(serial="dev-2", seq=0, zone="z2").results[0]
        second = self.scan(serial="dev-3", seq=0, zone="z2")
        self.assertEqual(first.case_id, second.results[0].case_id)
        case = [c for c in self.plaques.list_inspection_queue(site_id="s1")
                if c.case_id == first.case_id][0]
        self.assertEqual(2, case.event_count)
        self.assertEqual(1, len(self.plaques.list_supplements(first.case_id)))

    def test_replay_vs_fork_by_device_sequence(self):
        self.deploy_q1()
        payload = dict(seq=0, zone="z1", exhibit="e1", digest=D_A)
        self.scan(**payload)
        replay = self.scan(**payload)
        self.assertEqual(1, replay.replayed)
        self.assertEqual("replay", replay.results[0].verdict)

        fork = self.scan(seq=0, zone="z1", exhibit="e1", digest=D_B)
        self.assertEqual(1, fork.forked)
        self.assertEqual("fork", fork.results[0].verdict)
        # 分叉载荷不作为扫码事件保存
        rows = self.database.connection.execute(
            "SELECT COUNT(*) FROM scan_events WHERE device_pseudonym IN "
            "(SELECT device_pseudonym FROM scan_events) AND content_digest=?", (D_B,)).fetchone()[0]
        self.assertEqual(0, rows)

    def test_different_device_same_seq_is_neither_replay_nor_fork(self):
        self.deploy_q1()
        self.scan(serial="dev-1", seq=0)
        other = self.scan(serial="dev-2", seq=0)
        self.assertEqual(1, other.accepted)
        self.assertEqual(0, other.replayed)
        self.assertEqual(0, other.forked)

    def test_batch_request_id_replays_report_without_duplicate_inserts(self):
        self.deploy_q1()
        body = dict(device_serial="dev-1", site_id="s1", request_id="up-1", events=[
            {"plaque_code": "Q1", "event_seq": 0, "scanned_at": "2026-09-27T01:00:00Z",
             "zone_id": "z1", "exhibit_id": "e1", "content_digest": D_A}])
        first = self.plaques.upload_scans(**body)
        second = self.plaques.upload_scans(**body)
        self.assertEqual(1, first.accepted)
        self.assertEqual(1, second.accepted)
        self.assertEqual(1, self.database.connection.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0])
        with self.assertRaises(ConflictError):
            changed = dict(body)
            changed["events"] = [dict(body["events"][0], content_digest=D_B)]
            self.plaques.upload_scans(**changed)

    def test_raw_device_serial_never_stored_and_pseudonym_rotates_daily(self):
        self.deploy_q1()
        self.scan(serial="IMEI-99887766", seq=0, at="2026-09-27T01:00:00Z")
        self.scan(serial="IMEI-99887766", seq=1, at="2026-09-28T01:00:00Z")
        hits = self.database.connection.execute(
            "SELECT COUNT(*) FROM scan_events WHERE device_pseudonym='IMEI-99887766'").fetchone()[0]
        self.assertEqual(0, hits)
        names = [row[0] for row in self.database.connection.execute(
            "SELECT DISTINCT device_pseudonym FROM scan_events ORDER BY day")]
        self.assertEqual(2, len(names))
        self.assertNotEqual(names[0], names[1])


class InspectionFlowTest(PlaqueTestBase):
    def _open_case(self, kind="stale_position"):
        self.deploy_q1()
        overrides = {"stale_position": dict(zone="z2"),
                     "digest_mismatch": dict(digest=D_B),
                     "unknown_plaque": dict(code="ZZ", zone=None, exhibit=None, digest=None)}
        report = self.scan(serial="dev-2", seq=0, **overrides[kind])
        return report.results[0].case_id

    def test_claim_has_expiry_and_returns_to_queue(self):
        case_id = self._open_case()
        claim = self.plaques.claim_case(actor_id="op1", case_id=case_id)
        self.assertEqual("claimed", claim["status"])
        self.assertIn("claim_expires_at", claim)
        with self.assertRaises(ConflictError):
            self.plaques.claim_case(actor_id="op2", case_id=case_id)
        self.advance(self.claim_ttl + 1)
        again = self.plaques.claim_case(actor_id="op2", case_id=case_id)
        self.assertEqual("op2", again["claimed_by"])
        queue = self.plaques.list_inspection_queue(site_id="s1", status="open")
        self.assertEqual(0, len(queue))

    def test_resolve_requires_evidence_and_claim(self):
        case_id = self._open_case()
        with self.assertRaises(ConflictError):
            self.plaques.resolve_case(actor_id="op1", case_id=case_id,
                                     evidence_type="repost", evidence_ref="photo-1")
        self.plaques.claim_case(actor_id="op1", case_id=case_id)
        with self.assertRaises(ValidationError):
            self.plaques.resolve_case(actor_id="op1", case_id=case_id,
                                     evidence_type="unsupported", evidence_ref="photo-1")
        with self.assertRaises(ValidationError):
            self.plaques.resolve_case(actor_id="op1", case_id=case_id,
                                     evidence_type="repost", evidence_ref=" ")
        resolved = self.plaques.resolve_case(actor_id="op1", case_id=case_id,
                                             evidence_type="on_site_replacement",
                                             evidence_ref="work-order-77")
        self.assertTrue(resolved["protected"])

    def test_late_event_supplements_protected_conclusion_without_reopening(self):
        case_id = self._open_case()
        self.plaques.claim_case(actor_id="op1", case_id=case_id)
        self.plaques.resolve_case(actor_id="op1", case_id=case_id,
                                  evidence_type="repost", evidence_ref="photo-9")
        # 扫描发生在结案之前、回传迟到：只能按规则补充受保护结论
        report = self.scan(serial="dev-5", seq=0, zone="z2", at="2026-09-26T23:55:00Z")
        self.assertEqual(case_id, report.results[0].case_id)
        cases = {c.case_id: c for c in self.plaques.list_inspection_queue(
            site_id="s1", status="resolved")}
        self.assertEqual("resolved", cases[case_id].status)
        self.assertTrue(cases[case_id].protected)
        rules = [item["rule_code"] for item in self.plaques.list_supplements(case_id)]
        self.assertEqual(["late_event_after_resolution"], rules)
        # 默认队列只看待处理工单，受保护工单不会被迟到事件顶回队列
        self.assertEqual(
            [], [c for c in self.plaques.list_inspection_queue(site_id="s1") if c.case_id == case_id])
        with self.assertRaises(ConflictError):
            self.plaques.claim_case(actor_id="op2", case_id=case_id)

    def test_new_anomaly_after_resolution_opens_new_case(self):
        case_id = self._open_case()
        self.plaques.claim_case(actor_id="op1", case_id=case_id)
        self.plaques.resolve_case(actor_id="op1", case_id=case_id,
                                  evidence_type="repost", evidence_ref="photo-9")
        # 结案之后才发生的异常不是迟到事件，必须开新单
        report = self.scan(serial="dev-6", seq=0, zone="z2", at="2026-09-28T08:00:00Z")
        self.assertNotEqual(case_id, report.results[0].case_id)
        new_case = [c for c in self.plaques.list_inspection_queue(site_id="s1", status="open")
                    if c.case_id == report.results[0].case_id][0]
        self.assertEqual("stale_position", new_case.kind)
        self.assertEqual(1, new_case.event_count)

    def test_unfinished_case_survives_application_restart(self):
        import sqlite3

        case_id = self._open_case()
        self.plaques.claim_case(actor_id="op1", case_id=case_id)
        self.advance(10)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "restart.sqlite3"
            target = sqlite3.connect(path)
            self.database.connection.backup(target)
            target.close()
            self.database.close()

            self.database = Database(path)
            self.clock = FixedClock(T0 + timedelta(seconds=10))
            self.service = DomainService(self.database, self.clock)
            self.plaques = PlaqueService(self.database, self.clock, claim_ttl_seconds=self.claim_ttl)
            claimed = self.plaques.list_inspection_queue(site_id="s1", status="claimed")
            self.assertEqual(1, len(claimed))
            self.assertEqual(case_id, claimed[0].case_id)
            self.assertEqual("op1", claimed[0].claimed_by)
            valid, _ = self.service.verify_audit()
            self.assertTrue(valid)


class PositionAndStatisticsTest(PlaqueTestBase):
    def test_position_reports_anomaly_start(self):
        self.deploy_q1()
        self.scan(serial="dev-2", seq=0, zone="z2", at="2026-09-27T05:30:00Z")
        position = self.plaques.plaque_position("Q1")
        self.assertEqual("z1", position.zone_id)
        self.assertEqual("2026-09-27T05:30:00Z", position.anomaly_started_at)

    def test_anonymous_statistics_policy_and_counts(self):
        self.deploy_q1()
        self.scan(serial="dev-1", seq=0)
        self.scan(serial="dev-1", seq=0)
        self.scan(serial="dev-1", seq=1, zone="z2")
        self.scan(serial="dev-2", seq=0, digest=D_B)
        stats = self.plaques.anonymous_statistics(site_id="s1", day="2026-09-27")
        day = stats["days"][0]
        self.assertEqual(1, day["events_by_verdict"]["ok"])
        self.assertEqual(1, day["events_by_verdict"]["stale_position"])
        self.assertEqual(1, day["events_by_verdict"]["digest_mismatch"])
        self.assertEqual(2, day["distinct_devices"])
        self.assertEqual(1, day["replays"])
        self.assertEqual(0, day["forks"])
        self.assertFalse(stats["policy"]["raw_device_serial_stored"])
        self.assertFalse(stats["policy"]["cross_day_tracking_possible"])


if __name__ == "__main__":
    unittest.main()
