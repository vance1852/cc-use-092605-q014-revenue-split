"""收益归集服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS rev_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('station','operations','auditor')),
    station_id TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 结算依据批次：来源、适用范围、有效期、上限与使用顺序
CREATE TABLE IF NOT EXISTS revenue_sources (
    source_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('MARKET_ENERGY','GUARANTEED_PURCHASE','GREEN_CERTIFICATE','PEAK_SHAVING','CURTAILMENT_COMPENSATION')),
    cycle_id TEXT NOT NULL,
    station_id TEXT,
    eligible_units_json TEXT NOT NULL DEFAULT '[]',
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    use_order INTEGER NOT NULL,
    limit_type TEXT NOT NULL CHECK(limit_type IN ('MWH','CNY','NONE')),
    cap_amount TEXT,
    unit_price_cny_per_mwh TEXT,
    curtailment_rate_cny_per_mwh TEXT,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','retired')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES rev_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(cycle_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_sources_cycle
ON revenue_sources(cycle_id, use_order, source_id);

CREATE TABLE IF NOT EXISTS revenue_source_slots (
    source_id TEXT NOT NULL REFERENCES revenue_sources(source_id),
    slot_id TEXT NOT NULL,
    label TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    price_cny_per_mwh TEXT NOT NULL,
    eligible_units_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(source_id, slot_id)
);

-- 冻结的计量版本
CREATE TABLE IF NOT EXISTS measurement_versions (
    version_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    station_id TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    entries_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'frozen' CHECK(state IN ('frozen','confirmed','superseded')),
    supersedes_version_id TEXT REFERENCES measurement_versions(version_id),
    created_by TEXT NOT NULL REFERENCES rev_users(user_id),
    created_at TEXT NOT NULL,
    confirmed_by TEXT REFERENCES rev_users(user_id),
    confirmed_at TEXT,
    UNIQUE(cycle_id, content_sha256)
);

CREATE INDEX IF NOT EXISTS idx_measurements_cycle
ON measurement_versions(cycle_id, created_at);

-- 已确认损失电量（限电补偿的唯一依据）
CREATE TABLE IF NOT EXISTS confirmed_loss (
    version_id TEXT NOT NULL REFERENCES measurement_versions(version_id),
    unit_id TEXT NOT NULL,
    curtailment_mwh TEXT NOT NULL,
    PRIMARY KEY(version_id, unit_id)
);

-- 针对冻结计量版本的分摊运行（绑定来源快照，后续规则变更不影响旧运行）
CREATE TABLE IF NOT EXISTS settlement_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id TEXT NOT NULL REFERENCES measurement_versions(version_id),
    cycle_id TEXT NOT NULL,
    sources_snapshot_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'current' CHECK(state IN ('current','superseded')),
    created_by TEXT NOT NULL REFERENCES rev_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(version_id, input_sha256)
);

-- 每笔收益落到某来源的分摊行及其额度生命周期
CREATE TABLE IF NOT EXISTS revenue_applications (
    application_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES settlement_runs(run_id),
    version_id TEXT NOT NULL REFERENCES measurement_versions(version_id),
    cycle_id TEXT NOT NULL,
    source_id TEXT NOT NULL REFERENCES revenue_sources(source_id),
    unit_id TEXT NOT NULL,
    slot_id TEXT,
    basis TEXT NOT NULL CHECK(basis IN ('energy','loss')),
    quantity_mwh TEXT NOT NULL,
    unit_price_cny TEXT NOT NULL,
    amount_cny TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'reserved'
        CHECK(state IN ('reserved','settled','released','carried_forward')),
    reserved_mwh TEXT NOT NULL,
    settled_mwh TEXT NOT NULL DEFAULT '0',
    released_mwh TEXT NOT NULL DEFAULT '0',
    carried_mwh TEXT NOT NULL DEFAULT '0',
    reserved_cny TEXT NOT NULL,
    settled_cny TEXT NOT NULL DEFAULT '0',
    carried_cny TEXT NOT NULL DEFAULT '0',
    sequence_no INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_applications_cycle
ON revenue_applications(cycle_id, source_id, state);

CREATE INDEX IF NOT EXISTS idx_applications_version
ON revenue_applications(version_id, sequence_no);

CREATE TABLE IF NOT EXISTS application_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES revenue_applications(application_id),
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES rev_users(user_id),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_application_events
ON application_events(application_id, event_id);

CREATE TABLE IF NOT EXISTS settlement_confirmations (
    confirmation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id TEXT NOT NULL UNIQUE REFERENCES measurement_versions(version_id),
    actual_entries_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    confirmed_by TEXT NOT NULL REFERENCES rev_users(user_id),
    created_at TEXT NOT NULL
);

-- 人工改账：提交与复核分离，复核人不得是提交人，批准后生成新版本
CREATE TABLE IF NOT EXISTS manual_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES revenue_applications(application_id),
    delta_mwh TEXT NOT NULL,
    delta_cny TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','approved','rejected')),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES rev_users(user_id),
    created_at TEXT NOT NULL,
    reviewed_by TEXT REFERENCES rev_users(user_id),
    reviewed_at TEXT,
    review_note TEXT
);

CREATE TABLE IF NOT EXISTS rev_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS rev_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rev_audit_entity
ON rev_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(path), isolation_level=None, timeout=10, check_same_thread=check_same_thread
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
