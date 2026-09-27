"""实体标牌的身份、布设窗口、交接、扫码回传与巡检规则。"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import (
    Deployment,
    InspectionCase,
    Plaque,
    PlaquePosition,
    ScanResult,
    UploadReport,
)
from .storage import Database

EVIDENCE_TYPES = frozenset({"on_site_replacement", "repost", "false_alarm_review"})
ZONE_CATEGORY = "zone_registry"
EXHIBIT_CATEGORY = "activity_resource"
VERDICT_KIND = {
    "stale_position": "stale_position",
    "unknown_plaque": "unknown_plaque",
    "digest_mismatch": "digest_mismatch",
}
DEFAULT_CLAIM_TTL_SECONDS = 1800
CONTENT_TEXT_LIMIT = 500


def _parse_instant(value: str) -> datetime:
    """解析服务统一的 ISO-8601 UTC 时间字符串。"""

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValidationError("时间必须包含时区")
    return parsed.astimezone(timezone.utc)


class PlaqueService:
    """在基础服务之上实现标牌布设、回传分类与巡检闭环。"""

    def __init__(self, database: Database, clock: Clock | None = None,
                 claim_ttl_seconds: int = DEFAULT_CLAIM_TTL_SECONDS) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        self.claim_ttl_seconds = claim_ttl_seconds

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc)

    def _instant(self, value: str | None = None) -> str:
        if value is None:
            return self._format(self._now())
        return self._format(_parse_instant(value))

    @staticmethod
    def _format(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _actor(self, connection, actor_id: str):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require(self, actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _site_for_actor(self, connection, actor, site_id: str):
        site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if site is None:
            raise NotFoundError("场所不存在")
        if actor["organization_id"] != site["organization_id"] and actor["role"] != "admin":
            raise PermissionDenied("不能操作其他组织的场所")
        return site

    def _plaque(self, connection, plaque_id: str):
        row = connection.execute("SELECT * FROM plaques WHERE plaque_id=?", (plaque_id,)).fetchone()
        if row is None:
            raise NotFoundError("标牌不存在")
        return row

    def _active_deployment(self, connection, plaque_id: str):
        return connection.execute(
            "SELECT * FROM deployments WHERE plaque_id=? AND effective_to IS NULL",
            (plaque_id,),
        ).fetchone()

    def _reference(self, connection, site_id: str, category: str, key: str, label: str) -> None:
        """确认专区或展项已经作为结构化参考资料登记。"""

        row = connection.execute(
            "SELECT 1 FROM domain_records WHERE site_id=? AND category=? AND external_key=?",
            (site_id, category, key),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"{label}未在参考资料中登记")

    def _content(self, content_summary: str, content_digest: str) -> tuple[str, str]:
        content_summary = str(content_summary).strip()
        content_digest = str(content_digest).strip().lower()
        if not content_summary or len(content_summary) > CONTENT_TEXT_LIMIT:
            raise ValidationError(f"内容摘要不能为空且不能超过 {CONTENT_TEXT_LIMIT} 个字符")
        if len(content_digest) not in (32, 64) or any(c not in "0123456789abcdef" for c in content_digest):
            raise ValidationError("内容摘要必须提供十六进制摘要")
        return content_summary, content_digest

    def _day_salt(self, connection, site_id: str, day: str) -> str:
        connection.execute(
            "INSERT OR IGNORE INTO day_salts(site_id,day,salt,created_at) VALUES(?,?,?,?)",
            (site_id, day, os.urandom(32).hex(), self._instant()),
        )
        return connection.execute(
            "SELECT salt FROM day_salts WHERE site_id=? AND day=?", (site_id, day)
        ).fetchone()["salt"]

    @staticmethod
    def _pseudonym(salt: str, device_serial: str) -> str:
        return hashlib.sha256(f"{salt}:{device_serial}".encode("utf-8")).hexdigest()[:32]

    def _expire_stale_claims(self, connection) -> None:
        """按服务当前时间让过期领取失效，记录行仍保留原状态。"""

        connection.execute(
            "UPDATE inspection_cases SET status='open', claimed_by=NULL, claimed_at=NULL, claim_expires_at=NULL "
            "WHERE status='claimed' AND claim_expires_at IS NOT NULL AND claim_expires_at<?",
            (self._instant(),),
        )

    # ------------------------------------------------------------------
    # 标牌身份与布设
    # ------------------------------------------------------------------

    def register_plaque(self, *, request_id: str, actor_id: str, site_id: str,
                        plaque_id: str | None = None) -> dict[str, Any]:
        """为一块实体标牌分配终身不变的身份编号。"""

        payload = {"site_id": site_id, "plaque_id": plaque_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_actor(connection, actor, site_id)
            replayed = self._replayed_receipt(connection, request_id, "register_plaque", payload)
            if replayed is not None:
                return replayed
            plaque_id = plaque_id or uuid.uuid4().hex
            plaque_id = str(plaque_id).strip()
            if not plaque_id or len(plaque_id) > 64:
                raise ValidationError("plaque_id 不能为空且不能超过 64 个字符")
            now = self._instant()
            try:
                connection.execute(
                    "INSERT INTO plaques(plaque_id,site_id,retired,created_by,created_at) VALUES(?,?,0,?,?)",
                    (plaque_id, site_id, actor_id, now),
                )
            except Exception as exc:
                raise ConflictError("标牌编号已经存在") from exc
            append_event(connection, actor_id=actor_id, action="plaque.registered",
                         resource_type="plaque", resource_id=plaque_id,
                         detail={"site_id": site_id}, occurred_at=now)
            self._receipt(connection, request_id, "register_plaque",
                          {"site_id": site_id, "plaque_id": plaque_id}, "plaque", plaque_id, now)
            return {"replayed": False, "plaque_id": plaque_id}

    def deploy_plaque(self, *, request_id: str, actor_id: str, plaque_id: str,
                      zone_id: str, exhibit_id: str,
                      content_summary: str, content_digest: str,
                      effective_from: str | None = None) -> dict[str, Any]:
        """为新标牌建立首个带生效窗口的布设记录。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            plaque = self._plaque(connection, plaque_id)
            self._site_for_actor(connection, actor, plaque["site_id"])
            if plaque["retired"]:
                raise ConflictError("标牌已停用，不能重新布设")
            if self._active_deployment(connection, plaque_id) is not None:
                raise ConflictError("标牌存在生效中的布设，换位必须走交接流程")
            zone_id = str(zone_id).strip()
            exhibit_id = str(exhibit_id).strip()
            if not zone_id or not exhibit_id:
                raise ValidationError("专区与展项不能为空")
            self._reference(connection, plaque["site_id"], ZONE_CATEGORY, zone_id, "专区")
            self._reference(connection, plaque["site_id"], EXHIBIT_CATEGORY, exhibit_id, "展项")
            content_summary, content_digest = self._content(content_summary, content_digest)
            effective_from = self._instant(effective_from)
            payload = {"plaque_id": plaque_id, "zone_id": zone_id, "exhibit_id": exhibit_id,
                       "content_digest": content_digest, "effective_from": effective_from}
            replayed = self._replayed_receipt(connection, request_id, "deploy_plaque", payload)
            if replayed is not None:
                return replayed
            now = self._instant()
            deployment_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO deployments(deployment_id,plaque_id,zone_id,exhibit_id,content_summary,"
                "content_digest,effective_from,effective_to,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,NULL,?,?)",
                (deployment_id, plaque_id, zone_id, exhibit_id, content_summary,
                 content_digest, effective_from, actor_id, now),
            )
            append_event(connection, actor_id=actor_id, action="plaque.deployed",
                         resource_type="deployment", resource_id=deployment_id,
                         detail={"plaque_id": plaque_id, "zone_id": zone_id, "exhibit_id": exhibit_id,
                                 "content_digest": content_digest, "effective_from": effective_from},
                         occurred_at=now)
            self._receipt(connection, request_id, "deploy_plaque",
                          {"plaque_id": plaque_id, "zone_id": zone_id, "exhibit_id": exhibit_id,
                           "content_digest": content_digest, "effective_from": effective_from},
                          "deployment", deployment_id, now)
            return {"replayed": False, "deployment_id": deployment_id,
                    "plaque_id": plaque_id, "effective_from": effective_from}

    def request_relocation(self, *, request_id: str, actor_id: str, plaque_id: str,
                           zone_id: str, exhibit_id: str,
                           content_summary: str, content_digest: str) -> dict[str, Any]:
        """发起标牌换位申请，等待移出方与接收方依次交接。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            plaque = self._plaque(connection, plaque_id)
            self._site_for_actor(connection, actor, plaque["site_id"])
            current = self._active_deployment(connection, plaque_id)
            if current is None:
                raise ConflictError("标牌没有生效中的布设，不能申请换位")
            zone_id = str(zone_id).strip()
            exhibit_id = str(exhibit_id).strip()
            if not zone_id or not exhibit_id:
                raise ValidationError("专区与展项不能为空")
            self._reference(connection, plaque["site_id"], ZONE_CATEGORY, zone_id, "专区")
            self._reference(connection, plaque["site_id"], EXHIBIT_CATEGORY, exhibit_id, "展项")
            content_summary, content_digest = self._content(content_summary, content_digest)
            if (zone_id, exhibit_id, content_digest) == (
                    current["zone_id"], current["exhibit_id"], current["content_digest"]):
                raise ValidationError("目标位置和内容与当前布设一致，无需换位")
            payload = {"plaque_id": plaque_id, "zone_id": zone_id, "exhibit_id": exhibit_id,
                       "content_digest": content_digest}
            replayed = self._replayed_receipt(connection, request_id, "request_relocation", payload)
            if replayed is not None:
                return replayed
            pending = connection.execute(
                "SELECT relocation_id FROM relocations WHERE plaque_id=? AND status='pending'",
                (plaque_id,),
            ).fetchone()
            if pending is not None:
                raise ConflictError("该标牌已有待完成的交接")
            now = self._instant()
            relocation_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO relocations(relocation_id,plaque_id,old_deployment_id,zone_id,exhibit_id,"
                "content_summary,content_digest,requested_by,requested_at,status) "
                "VALUES(?,?,?,?,?,?,?,?,?, 'pending')",
                (relocation_id, plaque_id, current["deployment_id"], zone_id, exhibit_id,
                 content_summary, content_digest, actor_id, now),
            )
            append_event(connection, actor_id=actor_id, action="relocation.requested",
                         resource_type="relocation", resource_id=relocation_id,
                         detail={"plaque_id": plaque_id, "from_zone": current["zone_id"],
                                 "to_zone": zone_id, "to_exhibit": exhibit_id},
                         occurred_at=now)
            self._receipt(connection, request_id, "request_relocation",
                          {"plaque_id": plaque_id, "zone_id": zone_id, "exhibit_id": exhibit_id},
                          "relocation", relocation_id, now)
            return {"replayed": False, "relocation_id": relocation_id, "status": "pending"}

    def handover_relocation(self, *, actor_id: str, relocation_id: str, action: str) -> dict[str, Any]:
        """移出方释放、接收方签收，两步齐备换位才成立。"""

        if action not in ("release", "receive"):
            raise ValidationError("action 只能是 release 或 receive")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            row = connection.execute(
                "SELECT * FROM relocations WHERE relocation_id=?", (relocation_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("交接记录不存在")
            plaque = self._plaque(connection, row["plaque_id"])
            self._site_for_actor(connection, actor, plaque["site_id"])
            if row["status"] != "pending":
                raise ConflictError("交接已经结束")
            now = self._instant()
            if action == "release":
                if row["released_by"] is not None:
                    raise ConflictError("移出方已经完成释放")
                connection.execute(
                    "UPDATE relocations SET released_by=?, released_at=? WHERE relocation_id=?",
                    (actor_id, now, relocation_id),
                )
                append_event(connection, actor_id=actor_id, action="relocation.released",
                             resource_type="relocation", resource_id=relocation_id,
                             detail={"plaque_id": row["plaque_id"]}, occurred_at=now)
                return {"relocation_id": relocation_id, "status": "released"}
            if row["released_by"] is None:
                raise ConflictError("必须先由移出方释放，接收方才能签收")
            if row["released_by"] == actor_id:
                raise ConflictError("移出方与接收方不能是同一操作者")
            current = connection.execute(
                "SELECT * FROM deployments WHERE deployment_id=?",
                (row["old_deployment_id"],),
            ).fetchone()
            if current is None or current["effective_to"] is not None:
                raise ConflictError("原布设已失效，交接不能成立")
            deployment_id = uuid.uuid4().hex
            connection.execute(
                "UPDATE deployments SET effective_to=? WHERE deployment_id=?",
                (now, row["old_deployment_id"]),
            )
            connection.execute(
                "INSERT INTO deployments(deployment_id,plaque_id,zone_id,exhibit_id,content_summary,"
                "content_digest,effective_from,effective_to,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,NULL,?,?)",
                (deployment_id, row["plaque_id"], row["zone_id"], row["exhibit_id"],
                 row["content_summary"], row["content_digest"], now, actor_id, now),
            )
            connection.execute(
                "UPDATE relocations SET received_by=?, received_at=?, status='completed', "
                "completed_at=?, new_deployment_id=? WHERE relocation_id=?",
                (actor_id, now, now, deployment_id, relocation_id),
            )
            append_event(connection, actor_id=actor_id, action="relocation.completed",
                         resource_type="relocation", resource_id=relocation_id,
                         detail={"plaque_id": row["plaque_id"], "old_deployment_id": row["old_deployment_id"],
                                 "new_deployment_id": deployment_id,
                                 "from_zone": current["zone_id"], "to_zone": row["zone_id"],
                                 "from_exhibit": current["exhibit_id"], "to_exhibit": row["exhibit_id"]},
                         occurred_at=now)
            return {"relocation_id": relocation_id, "status": "completed",
                    "new_deployment_id": deployment_id, "effective_from": now}

    def cancel_relocation(self, *, actor_id: str, relocation_id: str) -> dict[str, Any]:
        """撤销尚未完成的交接。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            row = connection.execute(
                "SELECT * FROM relocations WHERE relocation_id=?", (relocation_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("交接记录不存在")
            plaque = self._plaque(connection, row["plaque_id"])
            self._site_for_actor(connection, actor, plaque["site_id"])
            if row["status"] != "pending":
                raise ConflictError("交接已经结束，不能撤销")
            now = self._instant()
            connection.execute(
                "UPDATE relocations SET status='cancelled', completed_at=? WHERE relocation_id=?",
                (now, relocation_id),
            )
            append_event(connection, actor_id=actor_id, action="relocation.cancelled",
                         resource_type="relocation", resource_id=relocation_id,
                         detail={"plaque_id": row["plaque_id"]}, occurred_at=now)
            return {"relocation_id": relocation_id, "status": "cancelled"}

    # ------------------------------------------------------------------
    # 终端离线回传
    # ------------------------------------------------------------------

    def upload_scans(self, *, device_serial: str, site_id: str,
                     events: list[dict[str, Any]], request_id: str | None = None) -> UploadReport:
        """接收一批终端扫码事件，按设备序号区分重放与分叉并分类入队。

        设备原始序号只参与当日加盐伪名计算，绝不落库；伪名按 UTC 日轮换，
        因此存储内容无法还原个人跨日浏览轨迹。
        """

        device_serial = str(device_serial or "").strip()
        if not device_serial or len(device_serial) > 128:
            raise ValidationError("device_serial 不能为空且不能超过 128 个字符")
        if not isinstance(events, list) or not events:
            raise ValidationError("events 必须是非空数组")
        normalized = []
        for event in events:
            if not isinstance(event, dict):
                raise ValidationError("扫码事件必须是对象")
            plaque_code = str(event.get("plaque_code", "")).strip()
            if not plaque_code or len(plaque_code) > 64:
                raise ValidationError("plaque_code 不能为空且不能超过 64 个字符")
            event_seq = event.get("event_seq")
            if not isinstance(event_seq, int) or isinstance(event_seq, bool) or event_seq < 0:
                raise ValidationError("event_seq 必须是非负整数")
            scanned_at = self._instant(event.get("scanned_at"))
            zone_id = event.get("zone_id")
            exhibit_id = event.get("exhibit_id")
            content_digest = event.get("content_digest")
            zone_id = str(zone_id).strip() if zone_id else None
            exhibit_id = str(exhibit_id).strip() if exhibit_id else None
            content_digest = str(content_digest).strip().lower() if content_digest else None
            if content_digest is not None and (
                    len(content_digest) not in (32, 64)
                    or any(c not in "0123456789abcdef" for c in content_digest)):
                raise ValidationError("content_digest 必须是十六进制摘要")
            normalized.append({"plaque_code": plaque_code, "event_seq": event_seq,
                               "scanned_at": scanned_at, "zone_id": zone_id,
                               "exhibit_id": exhibit_id, "content_digest": content_digest})

        with self.database.transaction(immediate=True) as connection:
            site = connection.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("场所不存在")
            batch_hash = digest({"events": normalized})
            if request_id:
                receipt = connection.execute(
                    "SELECT payload_hash, response_json FROM scan_upload_receipts WHERE request_id=?",
                    (str(request_id).strip(),),
                ).fetchone()
                if receipt:
                    if receipt["payload_hash"] != batch_hash:
                        raise ConflictError("request_id 已被不同内容使用")
                    return self._report_from_json(json.loads(receipt["response_json"]))
            salt_cache: dict[str, str] = {}
            accepted = 0
            replayed = 0
            forked = 0
            results: list[ScanResult] = []
            batch_seen: dict[tuple[str, int], tuple[str, str]] = {}
            received_at = self._instant()
            for event in normalized:
                day = event["scanned_at"][:10]
                salt = salt_cache.setdefault(day, self._day_salt(connection, site_id, day))
                pseudonym = self._pseudonym(salt, device_serial)
                payload = {"site_id": site_id, "plaque_code": event["plaque_code"],
                           "zone_id": event["zone_id"], "exhibit_id": event["exhibit_id"],
                           "content_digest": event["content_digest"],
                           "scanned_at": event["scanned_at"], "event_seq": event["event_seq"]}
                payload_hash = digest(payload)
                key = (day, pseudonym, event["event_seq"])
                existing = connection.execute(
                    "SELECT event_id, payload_hash FROM scan_events WHERE day=? AND device_pseudonym=? AND event_seq=?",
                    key,
                ).fetchone()
                duplicate_of = batch_seen.get((pseudonym, event["event_seq"]))
                if existing is None and duplicate_of is None:
                    verdict, plaque_id, deployment_id = self._classify(connection, site_id, event)
                    event_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO scan_events(event_id,site_id,day,device_pseudonym,event_seq,plaque_code,"
                        "plaque_id,zone_id,exhibit_id,content_digest,scanned_at,received_at,deployment_id,"
                        "verdict,payload_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (event_id, site_id, day, pseudonym, event["event_seq"], event["plaque_code"],
                         plaque_id, event["zone_id"], event["exhibit_id"], event["content_digest"],
                         event["scanned_at"], received_at, deployment_id, verdict, payload_hash),
                    )
                    case_id = None
                    if verdict in VERDICT_KIND:
                        case_id = self._route_case(connection, site_id, event, event_id,
                                                   verdict, plaque_id, received_at)
                    batch_seen[(pseudonym, event["event_seq"])] = (event_id, payload_hash)
                    accepted += 1
                    results.append(ScanResult(event_id, verdict, case_id, False))
                    continue
                if existing is not None:
                    reference_event = existing["event_id"]
                    reference_hash = existing["payload_hash"]
                else:
                    reference_event, reference_hash = duplicate_of
                if reference_hash == payload_hash:
                    replayed += 1
                    connection.execute(
                        "INSERT INTO scan_replays(replay_id,existing_event_id,day,received_at) "
                        "VALUES(?,?,?,?)",
                        (uuid.uuid4().hex, reference_event, day, received_at),
                    )
                    results.append(ScanResult(reference_event, "replay", None, True))
                else:
                    forked += 1
                    connection.execute(
                        "INSERT OR IGNORE INTO scan_forks(fork_id,site_id,day,device_pseudonym,event_seq,"
                        "existing_hash,incoming_hash,received_at) VALUES(?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, site_id, day, pseudonym, event["event_seq"],
                         reference_hash, payload_hash, received_at),
                    )
                    results.append(ScanResult("", "fork", None, True))
            report = UploadReport(accepted, replayed, forked, results)
            if request_id:
                connection.execute(
                    "INSERT INTO scan_upload_receipts(request_id,payload_hash,response_json,created_at) "
                    "VALUES(?,?,?,?)",
                    (str(request_id).strip(), batch_hash,
                     canonical_json(self._report_to_json(report)), received_at),
                )
            return report

    @staticmethod
    def _classify(connection, site_id: str, event: dict[str, Any]) -> tuple[str, str | None, str | None]:
        """按失效位置、未知标牌、摘要不符的优先级判定。"""

        plaque = connection.execute(
            "SELECT * FROM plaques WHERE plaque_id=? AND site_id=?",
            (event["plaque_code"], site_id),
        ).fetchone()
        if plaque is None or plaque["retired"]:
            return "unknown_plaque", None, None
        deployment = connection.execute(
            "SELECT * FROM deployments WHERE plaque_id=? AND effective_to IS NULL",
            (event["plaque_code"],),
        ).fetchone()
        if deployment is None:
            return "stale_position", plaque["plaque_id"], None
        if event["zone_id"] is not None and event["zone_id"] != deployment["zone_id"]:
            return "stale_position", plaque["plaque_id"], deployment["deployment_id"]
        if event["exhibit_id"] is not None and event["exhibit_id"] != deployment["exhibit_id"]:
            return "stale_position", plaque["plaque_id"], deployment["deployment_id"]
        if event["content_digest"] is not None and event["content_digest"] != deployment["content_digest"]:
            return "digest_mismatch", plaque["plaque_id"], deployment["deployment_id"]
        return "ok", plaque["plaque_id"], deployment["deployment_id"]

    def _route_case(self, connection, site_id: str, event: dict[str, Any], event_id: str,
                    verdict: str, plaque_id: str | None, now: str) -> str:
        """把异常事件送往对应巡检队列，受保护结论只接受迟到补充不被推翻。"""

        kind = VERDICT_KIND[verdict]
        open_case = connection.execute(
            "SELECT * FROM inspection_cases WHERE site_id=? AND plaque_code=? AND kind=? "
            "AND status<>'resolved' ORDER BY created_at DESC LIMIT 1",
            (site_id, event["plaque_code"], kind),
        ).fetchone()
        if open_case is not None:
            connection.execute(
                "UPDATE inspection_cases SET event_count=event_count+1, last_event_at=? WHERE case_id=?",
                (event["scanned_at"], open_case["case_id"]),
            )
            connection.execute(
                "INSERT OR IGNORE INTO case_supplements(supplement_id,case_id,event_id,rule_code,created_at) "
                "VALUES(?,?,?,?,?)",
                (uuid.uuid4().hex, open_case["case_id"], event_id, "repeat_anomaly_event", now),
            )
            return open_case["case_id"]
        # 只有扫描发生在结案之前、回传迟到的事件才按既定规则补充受保护结论；
        # 结案之后新发生的异常必须开新单。
        protected_case = connection.execute(
            "SELECT * FROM inspection_cases WHERE site_id=? AND plaque_code=? AND kind=? "
            "AND status='resolved' AND protected=1 AND resolved_at>=? "
            "ORDER BY resolved_at DESC LIMIT 1",
            (site_id, event["plaque_code"], kind, event["scanned_at"]),
        ).fetchone()
        if protected_case is not None:
            connection.execute(
                "UPDATE inspection_cases SET event_count=event_count+1, last_event_at=? WHERE case_id=?",
                (event["scanned_at"], protected_case["case_id"]),
            )
            connection.execute(
                "INSERT OR IGNORE INTO case_supplements(supplement_id,case_id,event_id,rule_code,created_at) "
                "VALUES(?,?,?,?,?)",
                (uuid.uuid4().hex, protected_case["case_id"], event_id,
                 "late_event_after_resolution", now),
            )
            return protected_case["case_id"]
        case_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO inspection_cases(case_id,site_id,plaque_code,plaque_id,kind,status,"
            "first_event_id,first_scanned_at,last_event_at,event_count,created_at) "
            "VALUES(?,?,?,?,?, 'open', ?,?,?,1,?)",
            (case_id, site_id, event["plaque_code"], plaque_id, kind,
             event_id, event["scanned_at"], event["scanned_at"], now),
        )
        return case_id

    @staticmethod
    def _report_to_json(report: UploadReport) -> dict[str, Any]:
        return {"accepted": report.accepted, "replayed": report.replayed, "forked": report.forked,
                "results": [result.__dict__ for result in report.results]}

    @staticmethod
    def _report_from_json(data: dict[str, Any]) -> UploadReport:
        results = [ScanResult(item["event_id"], item["verdict"], item["case_id"], item["duplicate"])
                   for item in data["results"]]
        return UploadReport(data["accepted"], data["replayed"], data["forked"], results)

    # ------------------------------------------------------------------
    # 巡检领取与结案
    # ------------------------------------------------------------------

    def list_inspection_queue(self, *, site_id: str, kind: str | None = None,
                              status: str | None = None) -> list[InspectionCase]:
        """列出巡检队列；过期领取按当前时间视为已退回待领取。"""

        if kind is not None and kind not in VERDICT_KIND.values():
            raise ValidationError("kind 不在允许范围内")
        if status is not None and status not in ("open", "claimed", "resolved"):
            raise ValidationError("status 不在允许范围内")
        # 先把已过期的领取持久化退回待领取，保证状态过滤与重启后的口径一致
        with self.database.transaction(immediate=True) as connection:
            self._expire_stale_claims(connection)
        query = "SELECT * FROM inspection_cases WHERE site_id=?"
        parameters: list[Any] = [site_id]
        if kind:
            query += " AND kind=?"
            parameters.append(kind)
        if status:
            query += " AND status=?"
            parameters.append(status)
        else:
            # 默认队列只看待处理工单；显式 status=resolved 才能查已结案
            query += " AND status<>'resolved'"
        query += " ORDER BY first_scanned_at, case_id"
        cases = []
        for row in self.database.connection.execute(query, parameters):
            cases.append(InspectionCase(
                row["case_id"], row["site_id"], row["plaque_code"], row["plaque_id"], row["kind"],
                row["status"], row["first_scanned_at"], row["last_event_at"], row["event_count"],
                row["claimed_by"], row["claimed_at"], row["claim_expires_at"],
                row["evidence_type"], row["evidence_ref"], row["resolved_by"], row["resolved_at"],
                bool(row["protected"]),
            ))
        return cases

    def claim_case(self, *, actor_id: str, case_id: str) -> dict[str, Any]:
        """领取未结工单，领取具有明确过期时间。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._expire_stale_claims(connection)
            row = connection.execute(
                "SELECT * FROM inspection_cases WHERE case_id=?", (case_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("巡检工单不存在")
            if row["status"] == "resolved":
                raise ConflictError("工单已经结案")
            now = self._now()
            if row["status"] == "claimed":
                if row["claimed_by"] == actor_id:
                    return {"case_id": case_id, "status": "claimed",
                            "claimed_by": actor_id, "claim_expires_at": row["claim_expires_at"],
                            "replayed": True}
                raise ConflictError("工单已被他人领取且未过期")
            expires = self._format(now + timedelta(seconds=self.claim_ttl_seconds))
            now_text = self._format(now)
            connection.execute(
                "UPDATE inspection_cases SET status='claimed', claimed_by=?, claimed_at=?, "
                "claim_expires_at=? WHERE case_id=?",
                (actor_id, now_text, expires, case_id),
            )
            append_event(connection, actor_id=actor_id, action="inspection.claimed",
                         resource_type="inspection_case", resource_id=case_id,
                         detail={"claim_expires_at": expires}, occurred_at=now_text)
            return {"case_id": case_id, "status": "claimed", "claimed_by": actor_id,
                    "claim_expires_at": expires, "replayed": False}

    def resolve_case(self, *, actor_id: str, case_id: str,
                     evidence_type: str, evidence_ref: str) -> dict[str, Any]:
        """凭现场更换、重新张贴或误报复核证据结案，结论受保护。"""

        if evidence_type not in EVIDENCE_TYPES:
            raise ValidationError("证据类型必须是现场更换、重新张贴或误报复核之一")
        evidence_ref = str(evidence_ref).strip()
        if not evidence_ref or len(evidence_ref) > 200:
            raise ValidationError("evidence_ref 不能为空且不能超过 200 个字符")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._expire_stale_claims(connection)
            row = connection.execute(
                "SELECT * FROM inspection_cases WHERE case_id=?", (case_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("巡检工单不存在")
            if row["status"] == "resolved":
                raise ConflictError("工单已经结案，迟到事件只能按规则补充")
            if row["status"] == "open":
                raise ConflictError("结案前必须先领取工单")
            if row["claimed_by"] != actor_id:
                raise ConflictError("只有领取人可以结案")
            now = self._instant()
            evidence_hash = digest({"evidence_type": evidence_type, "evidence_ref": evidence_ref,
                                    "case_id": case_id})
            connection.execute(
                "UPDATE inspection_cases SET status='resolved', evidence_type=?, evidence_ref=?, "
                "evidence_hash=?, resolved_by=?, resolved_at=?, protected=1, claim_expires_at=NULL "
                "WHERE case_id=?",
                (evidence_type, evidence_ref, evidence_hash, actor_id, now, case_id),
            )
            append_event(connection, actor_id=actor_id, action="inspection.resolved",
                         resource_type="inspection_case", resource_id=case_id,
                         detail={"kind": row["kind"], "plaque_code": row["plaque_code"],
                                 "evidence_type": evidence_type, "evidence_hash": evidence_hash},
                         occurred_at=now)
            return {"case_id": case_id, "status": "resolved", "protected": True,
                    "resolved_by": actor_id, "resolved_at": now}

    def list_supplements(self, case_id: str) -> list[dict[str, Any]]:
        """查看迟到或重复事件对工单的补充记录。"""

        rows = self.database.connection.execute(
            "SELECT supplement_id, case_id, event_id, rule_code, created_at "
            "FROM case_supplements WHERE case_id=? ORDER BY created_at, supplement_id",
            (case_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 运营查询
    # ------------------------------------------------------------------

    def get_plaque(self, plaque_id: str) -> Plaque:
        row = self.database.connection.execute(
            "SELECT * FROM plaques WHERE plaque_id=?", (plaque_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("标牌不存在")
        return Plaque(row["plaque_id"], row["site_id"], bool(row["retired"]), row["created_at"])

    def current_deployment(self, plaque_id: str) -> Deployment | None:
        row = self.database.connection.execute(
            "SELECT * FROM deployments WHERE plaque_id=? AND effective_to IS NULL",
            (plaque_id,),
        ).fetchone()
        if row is None:
            return None
        return Deployment(row["deployment_id"], row["plaque_id"], row["zone_id"], row["exhibit_id"],
                          row["content_digest"], row["effective_from"], row["effective_to"])

    def plaque_position(self, plaque_id: str) -> PlaquePosition:
        """返回标牌当前应在位置与异常起点。"""

        plaque = self.get_plaque(plaque_id)
        deployment = self.current_deployment(plaque_id)
        anomaly = self.database.connection.execute(
            "SELECT MIN(first_scanned_at) AS started_at FROM inspection_cases "
            "WHERE plaque_id=? AND status<>'resolved'",
            (plaque_id,),
        ).fetchone()
        relocated = self.database.connection.execute(
            "SELECT 1 FROM relocations WHERE plaque_id=? AND status='completed' LIMIT 1",
            (plaque_id,),
        ).fetchone() is not None
        return PlaquePosition(
            plaque_id,
            deployment.zone_id if deployment else None,
            deployment.exhibit_id if deployment else None,
            deployment.content_digest if deployment else None,
            deployment.effective_from if deployment else None,
            relocated,
            anomaly["started_at"],
        )

    def content_summary(self, deployment_id: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT deployment_id, plaque_id, zone_id, exhibit_id, content_summary, content_digest, "
            "effective_from, effective_to FROM deployments WHERE deployment_id=?",
            (deployment_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("布设记录不存在")
        return dict(row)

    def anonymous_statistics(self, *, site_id: str, day: str | None = None) -> dict[str, Any]:
        """按当日加盐伪名口径输出匿名统计，不暴露任何设备原始标识。"""

        connection = self.database.connection
        days: dict[str, dict[str, Any]] = {}

        def bucket(day_value: str) -> dict[str, Any]:
            return days.setdefault(day_value, {
                "day": day_value,
                "events_by_verdict": {},
                "distinct_devices": 0,
                "replays": 0,
                "forks": 0,
            })

        event_query = (
            "SELECT verdict, day, COUNT(*) AS events, COUNT(DISTINCT device_pseudonym) AS devices "
            "FROM scan_events WHERE site_id=?"
        )
        parameters: list[Any] = [site_id]
        if day:
            event_query += " AND day=?"
            parameters.append(day)
        event_query += " GROUP BY verdict, day"
        for row in connection.execute(event_query, parameters):
            bucket(row["day"])["events_by_verdict"][row["verdict"]] = row["events"]

        device_query = "SELECT day, COUNT(DISTINCT device_pseudonym) AS devices FROM scan_events WHERE site_id=?"
        device_parameters: list[Any] = [site_id]
        if day:
            device_query += " AND day=?"
            device_parameters.append(day)
        device_query += " GROUP BY day"
        for row in connection.execute(device_query, device_parameters):
            bucket(row["day"])["distinct_devices"] = row["devices"]

        fork_query = "SELECT day, COUNT(*) AS forks FROM scan_forks WHERE site_id=?"
        fork_parameters: list[Any] = [site_id]
        if day:
            fork_query += " AND day=?"
            fork_parameters.append(day)
        fork_query += " GROUP BY day"
        for row in connection.execute(fork_query, fork_parameters):
            bucket(row["day"])["forks"] = row["forks"]

        replay_query = (
            "SELECT e.day AS day, COUNT(*) AS replays FROM scan_replays r "
            "JOIN scan_events e ON e.event_id=r.existing_event_id WHERE e.site_id=?"
        )
        replay_parameters: list[Any] = [site_id]
        if day:
            replay_query += " AND e.day=?"
            replay_parameters.append(day)
        replay_query += " GROUP BY e.day"
        for row in connection.execute(replay_query, replay_parameters):
            bucket(row["day"])["replays"] = row["replays"]

        open_counts = {
            row["kind"]: row["count"]
            for row in connection.execute(
                "SELECT kind, COUNT(*) AS count FROM inspection_cases "
                "WHERE site_id=? AND status<>'resolved' GROUP BY kind",
                (site_id,),
            )
        }
        return {
            "policy": {
                "identifier": "per_day_salted_device_pseudonym",
                "salt_rotation": "each_utc_day",
                "raw_device_serial_stored": False,
                "cross_day_tracking_possible": False,
                "replay_dedup": "identical payload hash is counted once",
                "fork_handling": "conflicting payload is never stored",
            },
            "days": [days[key] for key in sorted(days)],
            "open_inspection_cases": open_counts,
        }

    # ------------------------------------------------------------------
    # 幂等回执
    # ------------------------------------------------------------------

    def _receipt(self, connection, request_id: str, action: str, payload: dict[str, Any],
                 resource_type: str, resource_id: str, now: str) -> None:
        request_id = str(request_id).strip()
        if not request_id:
            raise ValidationError("request_id 不能为空")
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, digest(payload), resource_type, resource_id,
             canonical_json({"resource_id": resource_id}), now),
        )

    def _replayed_receipt(self, connection, request_id: str | None, action: str,
                          payload: dict[str, Any]) -> dict[str, Any] | None:
        """命中既有幂等回执时返回重放结果，内容不同则冲突。"""

        if not request_id:
            return None
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (str(request_id).strip(),)
        ).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return {"replayed": True, row["resource_type"] + "_id": row["resource_id"]}
