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
CREATE TABLE IF NOT EXISTS plaques (
    plaque_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    retired INTEGER NOT NULL DEFAULT 0 CHECK(retired IN (0, 1)),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deployments (
    deployment_id TEXT PRIMARY KEY,
    plaque_id TEXT NOT NULL REFERENCES plaques(plaque_id),
    zone_id TEXT NOT NULL,
    exhibit_id TEXT NOT NULL,
    content_summary TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_deployments_active
    ON deployments(plaque_id) WHERE effective_to IS NULL;
CREATE TABLE IF NOT EXISTS relocations (
    relocation_id TEXT PRIMARY KEY,
    plaque_id TEXT NOT NULL REFERENCES plaques(plaque_id),
    old_deployment_id TEXT NOT NULL REFERENCES deployments(deployment_id),
    zone_id TEXT NOT NULL,
    exhibit_id TEXT NOT NULL,
    content_summary TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    requested_by TEXT NOT NULL REFERENCES actors(actor_id),
    requested_at TEXT NOT NULL,
    released_by TEXT REFERENCES actors(actor_id),
    released_at TEXT,
    received_by TEXT REFERENCES actors(actor_id),
    received_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending', 'completed', 'cancelled')),
    completed_at TEXT,
    new_deployment_id TEXT REFERENCES deployments(deployment_id)
);
CREATE TABLE IF NOT EXISTS day_salts (
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    day TEXT NOT NULL,
    salt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(site_id, day)
);
CREATE TABLE IF NOT EXISTS scan_events (
    event_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    day TEXT NOT NULL,
    device_pseudonym TEXT NOT NULL,
    event_seq INTEGER NOT NULL CHECK(event_seq >= 0),
    plaque_code TEXT NOT NULL,
    plaque_id TEXT,
    zone_id TEXT,
    exhibit_id TEXT,
    content_digest TEXT,
    scanned_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    deployment_id TEXT,
    verdict TEXT NOT NULL CHECK(verdict IN
        ('ok', 'stale_position', 'unknown_plaque', 'digest_mismatch')),
    payload_hash TEXT NOT NULL,
    UNIQUE(day, device_pseudonym, event_seq)
);
CREATE TABLE IF NOT EXISTS scan_replays (
    replay_id TEXT PRIMARY KEY,
    existing_event_id TEXT NOT NULL REFERENCES scan_events(event_id),
    day TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_forks (
    fork_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    day TEXT NOT NULL,
    device_pseudonym TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    existing_hash TEXT NOT NULL,
    incoming_hash TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE(day, device_pseudonym, event_seq)
);
CREATE TABLE IF NOT EXISTS inspection_cases (
    case_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    plaque_code TEXT NOT NULL,
    plaque_id TEXT,
    kind TEXT NOT NULL CHECK(kind IN
        ('stale_position', 'unknown_plaque', 'digest_mismatch')),
    status TEXT NOT NULL CHECK(status IN ('open', 'claimed', 'resolved')),
    first_event_id TEXT NOT NULL REFERENCES scan_events(event_id),
    first_scanned_at TEXT NOT NULL,
    last_event_at TEXT NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 1 CHECK(event_count >= 1),
    claimed_by TEXT REFERENCES actors(actor_id),
    claimed_at TEXT,
    claim_expires_at TEXT,
    evidence_type TEXT,
    evidence_ref TEXT,
    evidence_hash TEXT,
    resolved_by TEXT REFERENCES actors(actor_id),
    resolved_at TEXT,
    protected INTEGER NOT NULL DEFAULT 0 CHECK(protected IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cases_open
    ON inspection_cases(site_id, plaque_code, kind) WHERE status <> 'resolved';
CREATE TABLE IF NOT EXISTS case_supplements (
    supplement_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES inspection_cases(case_id),
    event_id TEXT NOT NULL REFERENCES scan_events(event_id),
    rule_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(case_id, event_id)
);
CREATE TABLE IF NOT EXISTS scan_upload_receipts (
    request_id TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
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
