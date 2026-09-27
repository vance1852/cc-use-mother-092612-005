"""定义基础服务在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Actor:
    """表示具有明确角色的后台操作者。"""

    actor_id: str
    display_name: str
    role: str
    organization_id: str
    active: bool


@dataclass(frozen=True)
class Site:
    """表示中医药文化活动组织下的业务场所。"""

    site_id: str
    organization_id: str
    name: str
    timezone_name: str
    version: int


@dataclass(frozen=True)
class DomainRecord:
    """表示已经持久化的领域资料记录。"""

    record_id: str
    site_id: str
    category: str
    external_key: str
    payload: dict[str, Any]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class WriteReceipt:
    """描述一次幂等写入的稳定结果。"""

    request_id: str
    resource_type: str
    resource_id: str
    replayed: bool


@dataclass(frozen=True)
class Plaque:
    """实体二维码标牌，身份不随布设位置改变。"""

    plaque_id: str
    site_id: str
    retired: bool
    created_at: str


@dataclass(frozen=True)
class Deployment:
    """标牌在生效窗口内对专区、展项与内容摘要的关联。"""

    deployment_id: str
    plaque_id: str
    zone_id: str
    exhibit_id: str
    content_digest: str
    effective_from: str
    effective_to: str | None


@dataclass(frozen=True)
class PlaquePosition:
    """标牌当前应在的位置及异常起点。"""

    plaque_id: str
    zone_id: str | None
    exhibit_id: str | None
    content_digest: str | None
    effective_from: str | None
    relocated: bool
    anomaly_started_at: str | None


@dataclass(frozen=True)
class ScanResult:
    """单条扫码回传的判定结果。"""

    event_id: str
    verdict: str
    case_id: str | None
    duplicate: bool


@dataclass(frozen=True)
class UploadReport:
    """一次批量回传的分类汇总。"""

    accepted: int
    replayed: int
    forked: int
    results: list[ScanResult]


@dataclass(frozen=True)
class InspectionCase:
    """巡检工单及其领取、结案状态。"""

    case_id: str
    site_id: str
    plaque_code: str
    plaque_id: str | None
    kind: str
    status: str
    first_scanned_at: str
    last_event_at: str
    event_count: int
    claimed_by: str | None
    claimed_at: str | None
    claim_expires_at: str | None
    evidence_type: str | None
    evidence_ref: str | None
    resolved_by: str | None
    resolved_at: str | None
    protected: bool
