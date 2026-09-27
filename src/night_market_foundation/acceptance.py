"""运行基础服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .plaques import PlaqueService
from .service import DomainService
from .storage import Database

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


def run() -> dict[str, object]:
    """执行一条完整登记链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = DomainService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        plaques = PlaqueService(database, service.clock, claim_ttl_seconds=1800)
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范活动机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="活动负责人", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-receiver", actor_id="admin-001", new_actor_id="operator-002",
                               display_name="接收专区负责人", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号活动站点", timezone_name="Asia/Shanghai")
        for request_id, category, key, name in (
                ("req-zone-a", "zone_registry", "zone-gan", "甘草专区"),
                ("req-zone-b", "zone_registry", "zone-huang", "黄芪专区"),
                ("req-ex-a", "activity_resource", "ex-gan", "甘草展项"),
                ("req-ex-b", "activity_resource", "ex-huang", "黄芪展项")):
            service.record_domain_data(request_id=request_id, actor_id="operator-001", site_id="site-001",
                                       category=category, external_key=key, data={"name": name})
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="organizer_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="organizer_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        # 标牌身份与首布设
        plaque = plaques.register_plaque(request_id="req-plaque", actor_id="operator-001",
                                         site_id="site-001", plaque_id="plaque-001")
        plaques.deploy_plaque(request_id="req-deploy", actor_id="operator-001", plaque_id="plaque-001",
                              zone_id="zone-gan", exhibit_id="ex-gan", content_summary="甘草科普",
                              content_digest=DIGEST_A, effective_from="2026-09-25T08:00:00Z")

        # 离线回传：正常、重放、分叉、未知标牌、摘要不符
        plaques.upload_scans(device_serial="terminal-A", site_id="site-001", request_id="req-scan-ok", events=[
            {"plaque_code": "plaque-001", "event_seq": 0, "scanned_at": "2026-09-25T09:00:00Z",
             "zone_id": "zone-gan", "exhibit_id": "ex-gan", "content_digest": DIGEST_A}])
        replayed_batch = plaques.upload_scans(device_serial="terminal-A", site_id="site-001",
                                              request_id="req-scan-ok", events=[
            {"plaque_code": "plaque-001", "event_seq": 0, "scanned_at": "2026-09-25T09:00:00Z",
             "zone_id": "zone-gan", "exhibit_id": "ex-gan", "content_digest": DIGEST_A}])
        fork_report = plaques.upload_scans(device_serial="terminal-A", site_id="site-001", events=[
            {"plaque_code": "plaque-001", "event_seq": 0, "scanned_at": "2026-09-25T09:00:00Z",
             "zone_id": "zone-gan", "exhibit_id": "ex-gan", "content_digest": DIGEST_B}])
        plaques.upload_scans(device_serial="terminal-B", site_id="site-001", events=[
            {"plaque_code": "plaque-unknown", "event_seq": 0, "scanned_at": "2026-09-25T09:05:00Z"},
            {"plaque_code": "plaque-001", "event_seq": 1, "scanned_at": "2026-09-25T09:06:00Z",
             "zone_id": "zone-gan", "exhibit_id": "ex-gan", "content_digest": DIGEST_B}])

        # 交接换位：移出方释放、接收方签收后旧位置立即失效
        relocation = plaques.request_relocation(request_id="req-relocate", actor_id="operator-001",
                                                 plaque_id="plaque-001", zone_id="zone-huang",
                                                 exhibit_id="ex-huang", content_summary="黄芪科普",
                                                 content_digest=DIGEST_C)
        plaques.handover_relocation(actor_id="operator-001", relocation_id=relocation["relocation_id"],
                                    action="release")
        handover = plaques.handover_relocation(actor_id="operator-002",
                                               relocation_id=relocation["relocation_id"], action="receive")
        stale_report = plaques.upload_scans(device_serial="terminal-C", site_id="site-001", events=[
            {"plaque_code": "plaque-001", "event_seq": 0, "scanned_at": "2026-09-25T10:00:00Z",
             "zone_id": "zone-gan"}])
        stale_case = stale_report.results[0].case_id

        # 现场处置发生在异常回传之后；推进服务时钟再领取与结案
        later = FixedClock(datetime(2026, 9, 25, 10, 30, tzinfo=timezone.utc))
        service.clock = later
        plaques.clock = later
        plaques.claim_case(actor_id="operator-001", case_id=stale_case)
        plaques.resolve_case(actor_id="operator-001", case_id=stale_case,
                             evidence_type="repost", evidence_ref="photo-evidence-001")
        # 扫描发生在结案之前、回传迟到的事件：只能按规则补充，不能推翻受保护结论
        late_report = plaques.upload_scans(device_serial="terminal-C", site_id="site-001", events=[
            {"plaque_code": "plaque-001", "event_seq": 1, "scanned_at": "2026-09-25T10:05:00Z",
             "zone_id": "zone-gan"}])

        position = plaques.plaque_position("plaque-001")
        queue = plaques.list_inspection_queue(site_id="site-001")
        supplements = plaques.list_supplements(stale_case)
        stats = plaques.anonymous_statistics(site_id="site-001")
        raw_serial_rows = database.connection.execute(
            "SELECT COUNT(*) FROM scan_events WHERE device_pseudonym='terminal-A'"
        ).fetchone()[0]
        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed,
                  "plaque_registered": plaque["plaque_id"],
                  "batch_replayed": replayed_batch.accepted == 1 and replayed_batch.replayed == 0,
                  "fork_detected": fork_report.forked == 1,
                  "handover_completed": handover["status"] == "completed",
                  "current_zone": position.zone_id,
                  "history_preserved": position.relocated,
                  "queue_kinds": sorted(case.kind for case in queue),
                  "late_event_supplemented": late_report.results[0].case_id == stale_case
                  and len(supplements) == 1,
                  "anonymous": stats["policy"]["raw_device_serial_stored"] is False
                  and raw_serial_rows == 0}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
