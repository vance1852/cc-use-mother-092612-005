"""运行基础服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .signage import SignageService
from .storage import Database

CLOCK = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
DIGEST_OLD = "a" * 64
DIGEST_MISMATCH = "b" * 64
DIGEST_NEW = "c" * 64


def run() -> dict[str, object]:
    """执行一条完整登记链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "acceptance.sqlite3"
        database = Database(path)
        service = SignageService(database, CLOCK)
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范活动机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="活动负责人", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-operator-2", actor_id="admin-001", new_actor_id="operator-002",
                               display_name="接收方操作员", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号活动站点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="organizer_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="organizer_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})
        records = service.list_domain_data("site-001")

        # 标牌身份与布设
        service.register_sign(request_id="req-sign", actor_id="operator-001", site_id="site-001",
                              sign_id="sign-001", label="黄芪展项二维码牌")
        service.deploy_sign(request_id="req-deploy", actor_id="operator-001", sign_id="sign-001",
                            zone_code="zone-herb", exhibit_code="exhibit-huangqi",
                            content_digest=DIGEST_OLD, effective_from="2026-09-25T00:00:00Z")
        # 离线回传：一条有效、一条摘要不符、一条同设备重复补传
        batch = service.upload_scans(request_id="req-scan", actor_id="operator-001", site_id="site-001",
                                     device_id="terminal-01", uploads=[
                                         {"device_sequence": 1, "sign_code": "sign-001",
                                          "content_digest": DIGEST_OLD,
                                          "occurred_at": "2026-09-25T07:30:00Z"},
                                         {"device_sequence": 2, "sign_code": "sign-001",
                                          "content_digest": DIGEST_MISMATCH,
                                          "occurred_at": "2026-09-25T07:35:00Z"},
                                         {"device_sequence": 2, "sign_code": "sign-001",
                                          "content_digest": DIGEST_MISMATCH,
                                          "occurred_at": "2026-09-25T07:35:00Z"},
                                     ])
        opened = service.list_inspections("site-001", queue_type="digest_mismatch")
        inspection_id = opened[0]["inspection_id"]
        service.claim_inspection(request_id="req-claim", actor_id="operator-001",
                                 inspection_id=inspection_id)
        # 换位交接：移出方发起，接收方确认
        transfer = service.begin_transfer(request_id="req-transfer", actor_id="operator-001",
                                          sign_id="sign-001", zone_code="zone-wellness",
                                          exhibit_code="exhibit-huangqi-new",
                                          content_digest=DIGEST_NEW)
        service.confirm_transfer(request_id="req-transfer-confirm", actor_id="operator-002",
                                 transfer_id=transfer.resource_id)
        database.close()

        # 模拟应用重启：未结巡检保持原处理状态
        database = Database(path)
        service = SignageService(database, CLOCK)
        status_after_restart = service.get_inspection(inspection_id)["status"]
        service.resolve_inspection(request_id="req-resolve", actor_id="operator-001",
                                   inspection_id=inspection_id, evidence_type="re_posting",
                                   evidence_ref="workorder-2026-001")
        placement = service.sign_placement("sign-001")
        statistics = service.scan_statistics("site-001")

        valid, event_count = service.verify_audit()
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed,
                  "signage": {
                      "batch_accepted": batch["summary"]["accepted"],
                      "batch_replayed": batch["summary"]["replayed"],
                      "mismatch_queue_opened": len(opened),
                      "status_after_restart": status_after_restart,
                      "final_status": service.get_inspection(inspection_id)["status"],
                      "placement_status": placement["status"],
                      "placement_zone": placement.get("zone_code"),
                      "valid_scans": statistics["totals"]["valid"],
                      "mismatch_scans": statistics["totals"]["digest_mismatch"],
                  }}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
