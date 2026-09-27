"""二维码标牌身份、布设窗口、离线回传分拣与巡检闭环。

本模块在基础层之上实现夜市场景的四条核心规则：

1. 标牌身份（sign_id）终身不变，布设记录带生效窗口关联专区、展项与内容摘要；
2. 换位必须经移出方发起、接收方确认两方交接，发起即旧位置失效，历史扫描不迁移；
3. 终端离线回传按设备序号区分重放与分叉，失效位置、未知标牌、摘要不符分别入巡检队列，
   且全链路不保存任何可还原个人浏览轨迹的原始标识；
4. 巡检任务领取后带明确过期时间，结案必须引用现场更换、重新张贴或误报复核证据，
   迟到事件只能作为补充挂在已结案任务下，不能推翻受保护的结论。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .audit import append_event, digest
from .clock import Clock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import WriteReceipt
from .service import DomainService
from .storage import Database

QUEUE_TYPES = ("invalid_position", "unknown_sign", "digest_mismatch")
CLASSIFICATIONS = ("valid",) + QUEUE_TYPES
EVIDENCE_TYPES = ("on_site_replacement", "re_posting", "false_report_review")
INSPECTION_STATUSES = ("open", "claimed", "resolved")
DEFAULT_CLAIM_TTL_MINUTES = 120
MAX_CLAIM_TTL_MINUTES = 24 * 60
MAX_BATCH_SIZE = 500
_HEX_DIGITS = frozenset("0123456789abcdef")


def format_timestamp(value: datetime) -> str:
    """生成固定微秒宽度的 UTC 时间文本，保证字典序即时间序。"""

    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def normalize_timestamp(value: str, field: str) -> str:
    """把外部时间规范为可比较的 UTC 文本。"""

    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValidationError(f"{field} 必须是合法的 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} 必须包含时区")
    return format_timestamp(parsed)


class SignageService(DomainService):
    """在基础层之上提供标牌身份、布设、回传分拣和巡检闭环。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        super().__init__(database, clock)

    def _now(self) -> str:
        return format_timestamp(self.clock.now())

    def _content_digest(self, value: str) -> str:
        value = str(value).strip().lower()
        if len(value) != 64 or any(char not in _HEX_DIGITS for char in value):
            raise ValidationError("content_digest 必须是 64 位十六进制摘要")
        return value

    def _site_row(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _sign_row(self, connection, sign_id: str):
        row = connection.execute("SELECT * FROM signs WHERE sign_id=?", (sign_id,)).fetchone()
        if row is None:
            raise NotFoundError("标牌不存在")
        return row

    def _site_actor(self, connection, actor_id: str, site_row, *roles: str):
        actor = self._actor(connection, actor_id)
        self._require(actor, *roles)
        if actor.organization_id != site_row["organization_id"] and actor.role != "admin":
            raise PermissionDenied("不能操作其他组织的场所")
        return actor

    # ------------------------------------------------------------------
    # 标牌身份与布设
    # ------------------------------------------------------------------

    def register_sign(self, *, request_id: str, actor_id: str, site_id: str,
                      sign_id: str, label: str) -> WriteReceipt:
        """登记一块实体标牌的终身身份。"""

        payload = {"actor_id": actor_id, "site_id": site_id, "sign_id": sign_id, "label": label}
        with self.database.transaction(immediate=True) as connection:
            site = self._site_row(connection, site_id)
            self._site_actor(connection, actor_id, site, "admin", "operator")
            sign_id = self._identifier(sign_id, "sign_id")
            label = self._text(label, "label")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO signs(sign_id,site_id,label,status,created_by,created_at) "
                        "VALUES(?,?,?,'active',?,?)",
                        (sign_id, site_id, label, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("标牌编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="sign.registered",
                             resource_type="sign", resource_id=sign_id,
                             detail={"site_id": site_id, "label": label}, occurred_at=self._now())
                return "sign", sign_id, {"sign_id": sign_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_sign", payload=payload, create=create)

    def deploy_sign(self, *, request_id: str, actor_id: str, sign_id: str,
                    zone_code: str, exhibit_code: str, content_digest: str,
                    effective_from: str, effective_to: str | None = None) -> WriteReceipt:
        """为标牌建立一条带生效窗口的布设记录。"""

        payload = {"actor_id": actor_id, "sign_id": sign_id, "zone_code": zone_code,
                   "exhibit_code": exhibit_code, "content_digest": content_digest,
                   "effective_from": effective_from, "effective_to": effective_to}
        with self.database.transaction(immediate=True) as connection:
            sign = self._sign_row(connection, sign_id)
            site = self._site_row(connection, sign["site_id"])
            self._site_actor(connection, actor_id, site, "admin", "operator")
            if sign["status"] != "active":
                raise ConflictError("标牌已停用，不能布设")
            zone_code = self._identifier(zone_code, "zone_code")
            exhibit_code = self._identifier(exhibit_code, "exhibit_code")
            content_digest = self._content_digest(content_digest)
            effective_from = normalize_timestamp(effective_from, "effective_from")
            if effective_to is not None:
                effective_to = normalize_timestamp(effective_to, "effective_to")
                if effective_to <= effective_from:
                    raise ValidationError("effective_to 必须晚于 effective_from")

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute(
                    "SELECT 1 FROM sign_transfers WHERE sign_id=? AND status='pending'", (sign_id,)
                ).fetchone():
                    raise ConflictError("标牌正在交接中，不能重复布设")
                now = self._now()
                # 窗口已经自然结束的布设先归档，再检查是否仍有生效中的布设
                connection.execute(
                    "UPDATE sign_deployments SET status='ended', ended_at=? "
                    "WHERE sign_id=? AND status='active' AND effective_to IS NOT NULL AND effective_to<=?",
                    (now, sign_id, now),
                )
                if connection.execute(
                    "SELECT 1 FROM sign_deployments WHERE sign_id=? AND status='active'", (sign_id,)
                ).fetchone():
                    raise ConflictError("标牌当前已有生效布设，请先换位或等待窗口结束")
                deployment_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO sign_deployments(deployment_id,sign_id,site_id,zone_code,exhibit_code,"
                    "content_digest,effective_from,effective_to,status,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,'active',?,?)",
                    (deployment_id, sign_id, sign["site_id"], zone_code, exhibit_code,
                     content_digest, effective_from, effective_to, actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="deployment.created",
                             resource_type="sign_deployment", resource_id=deployment_id,
                             detail={"sign_id": sign_id, "zone_code": zone_code,
                                     "exhibit_code": exhibit_code, "content_digest": content_digest,
                                     "effective_from": effective_from, "effective_to": effective_to},
                             occurred_at=now)
                return "sign_deployment", deployment_id, {"deployment_id": deployment_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="deploy_sign", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 换位交接
    # ------------------------------------------------------------------

    def begin_transfer(self, *, request_id: str, actor_id: str, sign_id: str,
                       zone_code: str, exhibit_code: str, content_digest: str) -> WriteReceipt:
        """移出方发起换位：旧位置立即失效，交接等待接收方确认。"""

        payload = {"actor_id": actor_id, "sign_id": sign_id, "zone_code": zone_code,
                   "exhibit_code": exhibit_code, "content_digest": content_digest}
        with self.database.transaction(immediate=True) as connection:
            sign = self._sign_row(connection, sign_id)
            site = self._site_row(connection, sign["site_id"])
            self._site_actor(connection, actor_id, site, "admin", "operator")
            zone_code = self._identifier(zone_code, "zone_code")
            exhibit_code = self._identifier(exhibit_code, "exhibit_code")
            content_digest = self._content_digest(content_digest)

            def create() -> tuple[str, str, dict[str, Any]]:
                deployment = connection.execute(
                    "SELECT * FROM sign_deployments WHERE sign_id=? AND status='active'", (sign_id,)
                ).fetchone()
                if deployment is None:
                    raise ConflictError("标牌当前没有生效布设，无法移出")
                if connection.execute(
                    "SELECT 1 FROM sign_transfers WHERE sign_id=? AND status='pending'", (sign_id,)
                ).fetchone():
                    raise ConflictError("标牌已有进行中的交接")
                now = self._now()
                # 旧位置立即失效，历史扫描仍归属原布设，不做迁移
                connection.execute(
                    "UPDATE sign_deployments SET status='ended', effective_to=?, ended_at=? "
                    "WHERE deployment_id=?",
                    (now, now, deployment["deployment_id"]),
                )
                transfer_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO sign_transfers(transfer_id,sign_id,site_id,from_deployment_id,"
                    "zone_code,exhibit_code,content_digest,status,initiated_by,initiated_at) "
                    "VALUES(?,?,?,?,?,?,?,'pending',?,?)",
                    (transfer_id, sign_id, sign["site_id"], deployment["deployment_id"],
                     zone_code, exhibit_code, content_digest, actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="sign.transfer_initiated",
                             resource_type="sign_transfer", resource_id=transfer_id,
                             detail={"sign_id": sign_id,
                                     "from_deployment_id": deployment["deployment_id"],
                                     "zone_code": zone_code, "exhibit_code": exhibit_code},
                             occurred_at=now)
                return "sign_transfer", transfer_id, {"transfer_id": transfer_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="begin_transfer", payload=payload, create=create)

    def confirm_transfer(self, *, request_id: str, actor_id: str, transfer_id: str) -> WriteReceipt:
        """接收方确认交接：新布设自确认时刻生效，换位方告成立。"""

        payload = {"actor_id": actor_id, "transfer_id": transfer_id}
        with self.database.transaction(immediate=True) as connection:
            transfer = connection.execute(
                "SELECT * FROM sign_transfers WHERE transfer_id=?", (transfer_id,)
            ).fetchone()
            if transfer is None:
                raise NotFoundError("交接单不存在")
            site = self._site_row(connection, transfer["site_id"])
            self._site_actor(connection, actor_id, site, "admin", "operator")

            def create() -> tuple[str, str, dict[str, Any]]:
                if transfer["status"] != "pending":
                    raise ConflictError("交接已完成，不能重复确认")
                if transfer["initiated_by"] == actor_id:
                    raise ConflictError("移出方与接收方不能是同一人")
                if connection.execute(
                    "SELECT 1 FROM sign_deployments WHERE sign_id=? AND status='active'",
                    (transfer["sign_id"],),
                ).fetchone():
                    raise ConflictError("标牌已存在生效布设，交接无法落位")
                now = self._now()
                deployment_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO sign_deployments(deployment_id,sign_id,site_id,zone_code,exhibit_code,"
                    "content_digest,effective_from,effective_to,status,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,NULL,'active',?,?)",
                    (deployment_id, transfer["sign_id"], transfer["site_id"], transfer["zone_code"],
                     transfer["exhibit_code"], transfer["content_digest"], now, actor_id, now),
                )
                connection.execute(
                    "UPDATE sign_transfers SET status='completed', confirmed_by=?, confirmed_at=?, "
                    "to_deployment_id=? WHERE transfer_id=?",
                    (actor_id, now, deployment_id, transfer_id),
                )
                append_event(connection, actor_id=actor_id, action="sign.transfer_completed",
                             resource_type="sign_transfer", resource_id=transfer_id,
                             detail={"sign_id": transfer["sign_id"], "deployment_id": deployment_id,
                                     "zone_code": transfer["zone_code"],
                                     "exhibit_code": transfer["exhibit_code"]},
                             occurred_at=now)
                return "sign_transfer", transfer_id, {"transfer_id": transfer_id,
                                                      "deployment_id": deployment_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="confirm_transfer", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 终端离线回传
    # ------------------------------------------------------------------

    def upload_scans(self, *, request_id: str, actor_id: str, site_id: str,
                     device_id: str, uploads: list[dict[str, Any]]) -> dict[str, Any]:
        """接收终端离线批次，按设备序号区分重放与分叉并分拣异常。

        回传条目只保留设备、标牌与内容摘要，不保存任何可还原个人浏览轨迹的原始标识。
        """

        if not isinstance(uploads, list) or not uploads:
            raise ValidationError("uploads 必须是非空数组")
        if len(uploads) > MAX_BATCH_SIZE:
            raise ValidationError(f"uploads 单次不能超过 {MAX_BATCH_SIZE} 条")
        payload = {"actor_id": actor_id, "site_id": site_id, "device_id": device_id,
                   "uploads": uploads}
        with self.database.transaction(immediate=True) as connection:
            site = self._site_row(connection, site_id)
            self._site_actor(connection, actor_id, site, "admin", "operator")
            device_id = self._identifier(device_id, "device_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                batch_id = uuid.uuid4().hex
                results = [self._ingest_one(connection, site_id, device_id, actor_id, item)
                           for item in uploads]
                summary = {"accepted": 0, "replayed": 0, "fork": 0, "rejected": 0,
                           "classifications": {name: 0 for name in CLASSIFICATIONS}}
                for item in results:
                    summary[item["status"]] += 1
                    if item["status"] == "accepted":
                        summary["classifications"][item["classification"]] += 1
                append_event(connection, actor_id=actor_id, action="scan_batch.received",
                             resource_type="scan_batch", resource_id=batch_id,
                             detail={"site_id": site_id, "device_id": device_id, **summary},
                             occurred_at=self._now())
                return "scan_batch", batch_id, {"batch_id": batch_id, "summary": summary,
                                                "results": results}

            receipt = self._idempotent(connection, request_id=request_id,
                                       action="upload_scans", payload=payload, create=create)
            row = connection.execute(
                "SELECT response_json FROM request_receipts WHERE request_id=?", (request_id,)
            ).fetchone()
            stored = json.loads(row["response_json"])
            return {"request_id": receipt.request_id, "replayed": receipt.replayed, **stored}

    def _ingest_one(self, connection, site_id: str, device_id: str,
                    actor_id: str, item: Any) -> dict[str, Any]:
        if not isinstance(item, dict):
            return {"device_sequence": None, "status": "rejected", "message": "条目必须是对象"}
        sequence = item.get("device_sequence")
        try:
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
                raise ValidationError("device_sequence 必须是正整数")
            sign_code = self._identifier(str(item.get("sign_code", "")), "sign_code")
            content_digest = self._content_digest(str(item.get("content_digest", "")))
            occurred_at = normalize_timestamp(str(item.get("occurred_at", "")), "occurred_at")
        except ValidationError as exc:
            return {"device_sequence": sequence, "status": "rejected", "message": str(exc)}

        payload_hash = digest({"device_id": device_id, "device_sequence": sequence,
                               "sign_code": sign_code, "content_digest": content_digest,
                               "occurred_at": occurred_at})
        existing = connection.execute(
            "SELECT payload_hash FROM scan_events WHERE site_id=? AND device_id=? AND device_sequence=?",
            (site_id, device_id, sequence),
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] == payload_hash:
                self._bump_device(connection, site_id, device_id, replayed=1)
                return {"device_sequence": sequence, "status": "replayed"}
            self._bump_device(connection, site_id, device_id, fork=1)
            append_event(connection, actor_id=actor_id, action="scan.fork_detected",
                         resource_type="device", resource_id=device_id,
                         detail={"site_id": site_id, "device_sequence": sequence},
                         occurred_at=self._now())
            return {"device_sequence": sequence, "status": "fork",
                    "message": "设备序号已被不同内容使用"}

        sign = connection.execute(
            "SELECT * FROM signs WHERE sign_id=? AND site_id=?", (sign_code, site_id)
        ).fetchone()
        sign_id = None
        deployment_id = None
        if sign is None:
            classification = "unknown_sign"
        else:
            sign_id = sign["sign_id"]
            deployment = connection.execute(
                "SELECT * FROM sign_deployments WHERE sign_id=? AND effective_from<=? "
                "AND (effective_to IS NULL OR ?<effective_to) "
                "ORDER BY effective_from DESC LIMIT 1",
                (sign_id, occurred_at, occurred_at),
            ).fetchone()
            if deployment is None:
                classification = "invalid_position"
            elif deployment["content_digest"] != content_digest:
                classification = "digest_mismatch"
                deployment_id = deployment["deployment_id"]
            else:
                classification = "valid"
                deployment_id = deployment["deployment_id"]

        event_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO scan_events(event_id,site_id,device_id,device_sequence,sign_code,sign_id,"
            "deployment_id,content_digest,payload_hash,classification,occurred_at,received_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, site_id, device_id, sequence, sign_code, sign_id, deployment_id,
             content_digest, payload_hash, classification, occurred_at, self._now()),
        )
        self._bump_device(connection, site_id, device_id, sequence=sequence, accepted=1)
        result: dict[str, Any] = {"device_sequence": sequence, "status": "accepted",
                                  "classification": classification}
        if classification != "valid":
            inspection_id, supplement = self._route_to_inspection(
                connection, site_id, classification, sign_id, sign_code,
                event_id, occurred_at, actor_id)
            result["inspection_id"] = inspection_id
            if supplement:
                result["supplement"] = True
        return result

    def _bump_device(self, connection, site_id: str, device_id: str, *, sequence: int | None = None,
                     accepted: int = 0, replayed: int = 0, fork: int = 0) -> None:
        row = connection.execute(
            "SELECT last_sequence FROM device_cursors WHERE site_id=? AND device_id=?",
            (site_id, device_id),
        ).fetchone()
        now = self._now()
        if row is None:
            connection.execute(
                "INSERT INTO device_cursors(site_id,device_id,last_sequence,accepted_count,"
                "replayed_count,fork_count,updated_at) VALUES(?,?,?,?,?,?,?)",
                (site_id, device_id, sequence or 0, accepted, replayed, fork, now),
            )
        else:
            connection.execute(
                "UPDATE device_cursors SET last_sequence=?, accepted_count=accepted_count+?, "
                "replayed_count=replayed_count+?, fork_count=fork_count+?, updated_at=? "
                "WHERE site_id=? AND device_id=?",
                (max(row["last_sequence"], sequence or 0), accepted, replayed, fork, now,
                 site_id, device_id),
            )

    def _route_to_inspection(self, connection, site_id: str, queue_type: str,
                             sign_id: str | None, sign_code: str, event_id: str,
                             occurred_at: str, actor_id: str) -> tuple[str, int]:
        """把异常事件送往对应巡检队列；迟到事件只补充，不推翻已结案结论。"""

        if sign_id is not None:
            condition = "sign_id=?"
            key: Any = sign_id
        else:
            condition = "sign_id IS NULL AND sign_code=?"
            key = sign_code
        open_row = connection.execute(
            f"SELECT * FROM inspections WHERE site_id=? AND queue_type=? AND {condition} "
            "AND status IN ('open','claimed') ORDER BY opened_at DESC LIMIT 1",
            (site_id, queue_type, key),
        ).fetchone()
        now = self._now()
        if open_row is not None:
            inspection_id, supplement = open_row["inspection_id"], 0
        else:
            resolved_row = connection.execute(
                f"SELECT * FROM inspections WHERE site_id=? AND queue_type=? AND {condition} "
                "AND status='resolved' ORDER BY resolved_at DESC LIMIT 1",
                (site_id, queue_type, key),
            ).fetchone()
            if resolved_row is not None and occurred_at <= resolved_row["resolved_at"]:
                # 迟到事件：按既定规则补充到已结案任务，受保护的结论不变
                inspection_id, supplement = resolved_row["inspection_id"], 1
            else:
                inspection_id, supplement = uuid.uuid4().hex, 0
                connection.execute(
                    "INSERT INTO inspections(inspection_id,site_id,queue_type,sign_id,sign_code,"
                    "status,opened_at) VALUES(?,?,?,?,?,'open',?)",
                    (inspection_id, site_id, queue_type, sign_id, sign_code, now),
                )
                append_event(connection, actor_id=actor_id, action="inspection.opened",
                             resource_type="inspection", resource_id=inspection_id,
                             detail={"site_id": site_id, "queue_type": queue_type,
                                     "sign_id": sign_id, "sign_code": sign_code},
                             occurred_at=now)
        connection.execute(
            "INSERT INTO inspection_events(inspection_id,event_id,supplement,attached_at) "
            "VALUES(?,?,?,?)",
            (inspection_id, event_id, supplement, now),
        )
        if supplement:
            append_event(connection, actor_id=actor_id, action="inspection.supplemented",
                         resource_type="inspection", resource_id=inspection_id,
                         detail={"event_id": event_id, "occurred_at": occurred_at},
                         occurred_at=now)
        return inspection_id, supplement

    # ------------------------------------------------------------------
    # 巡检任务
    # ------------------------------------------------------------------

    def claim_inspection(self, *, request_id: str, actor_id: str, inspection_id: str,
                         ttl_minutes: int | None = None) -> WriteReceipt:
        """领取巡检任务并明确过期时间；已过期认领可被重新领取。"""

        payload = {"actor_id": actor_id, "inspection_id": inspection_id, "ttl_minutes": ttl_minutes}
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM inspections WHERE inspection_id=?", (inspection_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("巡检任务不存在")
            site = self._site_row(connection, row["site_id"])
            self._site_actor(connection, actor_id, site, "admin", "operator", "reviewer")
            ttl = DEFAULT_CLAIM_TTL_MINUTES if ttl_minutes is None else ttl_minutes
            if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= MAX_CLAIM_TTL_MINUTES:
                raise ValidationError(f"ttl_minutes 必须是 1 到 {MAX_CLAIM_TTL_MINUTES} 之间的整数")

            def create() -> tuple[str, str, dict[str, Any]]:
                now_dt = self.clock.now().astimezone(timezone.utc)
                now = format_timestamp(now_dt)
                if row["status"] == "resolved":
                    raise ConflictError("巡检任务已结案")
                if row["status"] == "claimed" and row["claim_expires_at"] > now:
                    raise ConflictError("巡检任务已被领取且尚未过期")
                expires_at = format_timestamp(now_dt + timedelta(minutes=ttl))
                connection.execute(
                    "UPDATE inspections SET status='claimed', claimed_by=?, claimed_at=?, "
                    "claim_expires_at=? WHERE inspection_id=?",
                    (actor_id, now, expires_at, inspection_id),
                )
                append_event(connection, actor_id=actor_id, action="inspection.claimed",
                             resource_type="inspection", resource_id=inspection_id,
                             detail={"claimed_by": actor_id, "claim_expires_at": expires_at},
                             occurred_at=now)
                return "inspection", inspection_id, {"inspection_id": inspection_id,
                                                     "claim_expires_at": expires_at}

            return self._idempotent(connection, request_id=request_id,
                                    action="claim_inspection", payload=payload, create=create)

    def resolve_inspection(self, *, request_id: str, actor_id: str, inspection_id: str,
                           evidence_type: str, evidence_ref: str,
                           note: str | None = None) -> WriteReceipt:
        """结案：必须由领取人在认领有效期内引用现场证据。"""

        payload = {"actor_id": actor_id, "inspection_id": inspection_id,
                   "evidence_type": evidence_type, "evidence_ref": evidence_ref, "note": note}
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM inspections WHERE inspection_id=?", (inspection_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("巡检任务不存在")
            site = self._site_row(connection, row["site_id"])
            self._site_actor(connection, actor_id, site, "admin", "operator", "reviewer")
            if evidence_type not in EVIDENCE_TYPES:
                raise ValidationError("evidence_type 必须是现场更换、重新张贴或误报复核之一")
            evidence_ref = self._text(evidence_ref, "evidence_ref")
            if note is not None:
                note = self._text(note, "note")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                if row["status"] == "resolved":
                    raise ConflictError("巡检任务已结案")
                if row["status"] != "claimed":
                    raise ConflictError("巡检任务尚未领取")
                if row["claimed_by"] != actor_id:
                    raise PermissionDenied("只能由领取人结案")
                if row["claim_expires_at"] <= now:
                    raise ConflictError("认领已过期，请重新领取后再结案")
                connection.execute(
                    "UPDATE inspections SET status='resolved', resolved_by=?, resolved_at=?, "
                    "evidence_type=?, evidence_ref=?, resolution_note=? WHERE inspection_id=?",
                    (actor_id, now, evidence_type, evidence_ref, note, inspection_id),
                )
                append_event(connection, actor_id=actor_id, action="inspection.resolved",
                             resource_type="inspection", resource_id=inspection_id,
                             detail={"queue_type": row["queue_type"], "evidence_type": evidence_type,
                                     "evidence_ref": evidence_ref},
                             occurred_at=now)
                return "inspection", inspection_id, {"inspection_id": inspection_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="resolve_inspection", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 运营查询
    # ------------------------------------------------------------------

    def sign_placement(self, sign_id: str) -> dict[str, Any]:
        """查询标牌当前应在的位置。"""

        connection = self.database.connection
        sign = self._sign_row(connection, sign_id)
        deployment = connection.execute(
            "SELECT * FROM sign_deployments WHERE sign_id=? AND status='active'", (sign_id,)
        ).fetchone()
        if deployment is not None:
            return {"sign_id": sign_id, "status": "deployed",
                    "zone_code": deployment["zone_code"],
                    "exhibit_code": deployment["exhibit_code"],
                    "content_digest": deployment["content_digest"],
                    "effective_from": deployment["effective_from"],
                    "effective_to": deployment["effective_to"]}
        transfer = connection.execute(
            "SELECT * FROM sign_transfers WHERE sign_id=? AND status='pending'", (sign_id,)
        ).fetchone()
        if transfer is not None:
            return {"sign_id": sign_id, "status": "in_transit",
                    "transfer_id": transfer["transfer_id"],
                    "target_zone_code": transfer["zone_code"],
                    "target_exhibit_code": transfer["exhibit_code"],
                    "initiated_at": transfer["initiated_at"]}
        return {"sign_id": sign_id, "status": "undeployed"}

    def sign_anomaly_origin(self, sign_id: str) -> dict[str, Any]:
        """查询标牌异常的起点（最早一条异常扫描）。"""

        connection = self.database.connection
        sign = self._sign_row(connection, sign_id)
        row = connection.execute(
            "SELECT e.*, ie.inspection_id AS inspection_id FROM scan_events e "
            "LEFT JOIN inspection_events ie ON ie.event_id=e.event_id "
            "WHERE e.site_id=? AND e.sign_id=? AND e.classification<>'valid' "
            "ORDER BY e.occurred_at, e.received_at, e.event_id LIMIT 1",
            (sign["site_id"], sign_id),
        ).fetchone()
        if row is None:
            return {"sign_id": sign_id, "origin": None}
        return {"sign_id": sign_id,
                "origin": {"classification": row["classification"],
                           "occurred_at": row["occurred_at"],
                           "received_at": row["received_at"],
                           "device_id": row["device_id"],
                           "sign_code": row["sign_code"],
                           "deployment_id": row["deployment_id"],
                           "inspection_id": row["inspection_id"]}}

    def scan_statistics(self, site_id: str) -> dict[str, Any]:
        """匿名统计口径：只有聚合计数，不含任何个人标识。"""

        connection = self.database.connection
        self._site_row(connection, site_id)
        totals = {name: 0 for name in CLASSIFICATIONS}
        for row in connection.execute(
            "SELECT classification, COUNT(*) AS count FROM scan_events WHERE site_id=? "
            "GROUP BY classification", (site_id,)
        ):
            totals[row["classification"]] = row["count"]

        per_sign: dict[str, dict[str, Any]] = {}
        for row in connection.execute(
            "SELECT sign_code, sign_id, classification, COUNT(*) AS count FROM scan_events "
            "WHERE site_id=? GROUP BY sign_code, sign_id, classification", (site_id,)
        ):
            entry = per_sign.setdefault(
                row["sign_code"],
                {"sign_code": row["sign_code"], "sign_id": row["sign_id"],
                 **{name: 0 for name in CLASSIFICATIONS}})
            entry[row["classification"]] = row["count"]

        per_zone = [
            {"zone_code": row["zone_code"], "exhibit_code": row["exhibit_code"],
             "valid": row["count"]}
            for row in connection.execute(
                "SELECT d.zone_code, d.exhibit_code, COUNT(*) AS count FROM scan_events e "
                "JOIN sign_deployments d ON d.deployment_id=e.deployment_id "
                "WHERE e.site_id=? AND e.classification='valid' "
                "GROUP BY d.zone_code, d.exhibit_code ORDER BY d.zone_code, d.exhibit_code",
                (site_id,),
            )
        ]

        devices = [
            {"device_id": row["device_id"], "last_sequence": row["last_sequence"],
             "accepted": row["accepted_count"], "replayed": row["replayed_count"],
             "forked": row["fork_count"]}
            for row in connection.execute(
                "SELECT * FROM device_cursors WHERE site_id=? ORDER BY device_id", (site_id,)
            )
        ]

        inspections = {queue: {status: 0 for status in INSPECTION_STATUSES}
                       for queue in QUEUE_TYPES}
        for row in connection.execute(
            "SELECT queue_type, status, COUNT(*) AS count FROM inspections WHERE site_id=? "
            "GROUP BY queue_type, status", (site_id,)
        ):
            inspections[row["queue_type"]][row["status"]] = row["count"]

        return {"site_id": site_id, "totals": totals,
                "per_sign": sorted(per_sign.values(), key=lambda item: item["sign_code"]),
                "per_zone": per_zone, "devices": devices, "inspections": inspections}

    def list_inspections(self, site_id: str, status: str | None = None,
                         queue_type: str | None = None) -> list[dict[str, Any]]:
        """按队列与状态列出巡检任务。"""

        connection = self.database.connection
        self._site_row(connection, site_id)
        if status is not None and status not in INSPECTION_STATUSES:
            raise ValidationError("status 不在允许范围内")
        if queue_type is not None and queue_type not in QUEUE_TYPES:
            raise ValidationError("queue_type 不在允许范围内")
        parameters: list[Any] = [site_id]
        query = "SELECT * FROM inspections WHERE site_id=?"
        if queue_type:
            query += " AND queue_type=?"
            parameters.append(queue_type)
        if status:
            query += " AND status=?"
            parameters.append(status)
        query += " ORDER BY opened_at, inspection_id"
        now = self._now()
        return [self._inspection_dict(connection, row, now)
                for row in connection.execute(query, parameters)]

    def get_inspection(self, inspection_id: str) -> dict[str, Any]:
        """查看巡检任务详情及其关联的扫描事件。"""

        connection = self.database.connection
        row = connection.execute(
            "SELECT * FROM inspections WHERE inspection_id=?", (inspection_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("巡检任务不存在")
        item = self._inspection_dict(connection, row, self._now())
        item["events"] = [
            {"event_id": event["event_id"], "classification": event["classification"],
             "sign_code": event["sign_code"], "device_id": event["device_id"],
             "device_sequence": event["device_sequence"], "occurred_at": event["occurred_at"],
             "received_at": event["received_at"], "supplement": bool(event["supplement"]),
             "attached_at": event["attached_at"]}
            for event in connection.execute(
                "SELECT e.*, ie.supplement, ie.attached_at FROM inspection_events ie "
                "JOIN scan_events e ON e.event_id=ie.event_id "
                "WHERE ie.inspection_id=? ORDER BY ie.id", (inspection_id,)
            )
        ]
        return item

    def _inspection_dict(self, connection, row, now: str) -> dict[str, Any]:
        counts = connection.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(supplement), 0) AS supplements "
            "FROM inspection_events WHERE inspection_id=?",
            (row["inspection_id"],),
        ).fetchone()
        return {
            "inspection_id": row["inspection_id"],
            "site_id": row["site_id"],
            "queue_type": row["queue_type"],
            "sign_id": row["sign_id"],
            "sign_code": row["sign_code"],
            "status": row["status"],
            "opened_at": row["opened_at"],
            "claimed_by": row["claimed_by"],
            "claimed_at": row["claimed_at"],
            "claim_expires_at": row["claim_expires_at"],
            "claim_expired": bool(row["status"] == "claimed"
                                  and row["claim_expires_at"] <= now),
            "resolved_by": row["resolved_by"],
            "resolved_at": row["resolved_at"],
            "evidence_type": row["evidence_type"],
            "evidence_ref": row["evidence_ref"],
            "resolution_note": row["resolution_note"],
            "event_count": counts["total"],
            "supplement_count": counts["supplements"],
        }
