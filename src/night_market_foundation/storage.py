"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signs (
    sign_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    label TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'retired')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sign_deployments (
    deployment_id TEXT PRIMARY KEY,
    sign_id TEXT NOT NULL REFERENCES signs(sign_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    zone_code TEXT NOT NULL,
    exhibit_code TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    status TEXT NOT NULL CHECK(status IN ('active', 'ended')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS sign_deployments_one_active
    ON sign_deployments(sign_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS sign_transfers (
    transfer_id TEXT PRIMARY KEY,
    sign_id TEXT NOT NULL REFERENCES signs(sign_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    from_deployment_id TEXT NOT NULL REFERENCES sign_deployments(deployment_id),
    to_deployment_id TEXT REFERENCES sign_deployments(deployment_id),
    zone_code TEXT NOT NULL,
    exhibit_code TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'completed')),
    initiated_by TEXT NOT NULL REFERENCES actors(actor_id),
    confirmed_by TEXT REFERENCES actors(actor_id),
    initiated_at TEXT NOT NULL,
    confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS sign_transfers_pending
    ON sign_transfers(sign_id) WHERE status='pending';
CREATE TABLE IF NOT EXISTS device_cursors (
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    device_id TEXT NOT NULL,
    last_sequence INTEGER NOT NULL,
    accepted_count INTEGER NOT NULL,
    replayed_count INTEGER NOT NULL,
    fork_count INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (site_id, device_id)
);
CREATE TABLE IF NOT EXISTS scan_events (
    event_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    device_id TEXT NOT NULL,
    device_sequence INTEGER NOT NULL,
    sign_code TEXT NOT NULL,
    sign_id TEXT,
    deployment_id TEXT REFERENCES sign_deployments(deployment_id),
    content_digest TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    classification TEXT NOT NULL
        CHECK(classification IN ('valid', 'invalid_position', 'unknown_sign', 'digest_mismatch')),
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE(site_id, device_id, device_sequence)
);
CREATE TABLE IF NOT EXISTS inspections (
    inspection_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    queue_type TEXT NOT NULL CHECK(queue_type IN ('invalid_position', 'unknown_sign', 'digest_mismatch')),
    sign_id TEXT,
    sign_code TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'claimed', 'resolved')),
    opened_at TEXT NOT NULL,
    claimed_by TEXT,
    claimed_at TEXT,
    claim_expires_at TEXT,
    resolved_by TEXT,
    resolved_at TEXT,
    evidence_type TEXT CHECK(evidence_type IN ('on_site_replacement', 're_posting', 'false_report_review')),
    evidence_ref TEXT,
    resolution_note TEXT
);
CREATE INDEX IF NOT EXISTS inspections_lookup
    ON inspections(site_id, queue_type, sign_code, status);
CREATE TABLE IF NOT EXISTS inspection_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inspection_id TEXT NOT NULL REFERENCES inspections(inspection_id),
    event_id TEXT NOT NULL REFERENCES scan_events(event_id),
    supplement INTEGER NOT NULL CHECK(supplement IN (0, 1)),
    attached_at TEXT NOT NULL,
    UNIQUE(inspection_id, event_id)
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
