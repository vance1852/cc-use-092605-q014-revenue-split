"""收益归集服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS revenue_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('station','business','finance','auditor')),
    station_id TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS units (
    unit_id TEXT PRIMARY KEY,
    station_id TEXT NOT NULL,
    name TEXT NOT NULL,
    capacity_mw TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS periods (
    period_id TEXT PRIMARY KEY,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','closed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(start_date, end_date)
);

CREATE TABLE IF NOT EXISTS revenue_sources (
    source_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL CHECK(source_type IN
        ('market','guaranteed','green_certificate','peak_reward','curtailment_comp')),
    usage_order INTEGER NOT NULL,
    scope_unit_ids TEXT NOT NULL DEFAULT '',
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    cap_mwh TEXT,
    unit_price_cny TEXT,
    time_prices_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','retired')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES revenue_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sources_validity
ON revenue_sources(state, valid_from, valid_to);

CREATE TABLE IF NOT EXISTS metering_versions (
    metering_version_id TEXT PRIMARY KEY,
    period_id TEXT NOT NULL REFERENCES periods(period_id),
    content_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'frozen'
        CHECK(state IN ('frozen','confirmed','adjusted','superseded')),
    supersedes_version_id TEXT REFERENCES metering_versions(metering_version_id),
    submitted_by TEXT NOT NULL REFERENCES revenue_users(user_id),
    submitted_at TEXT NOT NULL,
    confirmed_at TEXT,
    UNIQUE(period_id, content_sha256)
);

CREATE TABLE IF NOT EXISTS metering_lines (
    line_id TEXT NOT NULL,
    metering_version_id TEXT NOT NULL REFERENCES metering_versions(metering_version_id),
    unit_id TEXT NOT NULL REFERENCES units(unit_id),
    energy_kind TEXT NOT NULL CHECK(energy_kind IN ('generated','curtailed')),
    time_bucket TEXT NOT NULL CHECK(time_bucket IN ('PEAK','FLAT','VALLEY')),
    mwh TEXT NOT NULL,
    loss_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(loss_confirmed IN (0,1)),
    PRIMARY KEY(metering_version_id, line_id)
);

CREATE INDEX IF NOT EXISTS idx_lines_unit
ON metering_lines(unit_id, energy_kind);

CREATE TABLE IF NOT EXISTS allocation_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    metering_version_id TEXT NOT NULL REFERENCES metering_versions(metering_version_id),
    period_id TEXT NOT NULL REFERENCES periods(period_id),
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'reserved'
        CHECK(state IN ('reserved','settled','partially_settled','failed','cancelled','carried','adjusted')),
    created_by TEXT NOT NULL REFERENCES revenue_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_version_input
ON allocation_runs(metering_version_id, input_sha256);

CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES allocation_runs(run_id),
    metering_version_id TEXT NOT NULL,
    period_id TEXT NOT NULL,
    line_id TEXT NOT NULL,
    source_id TEXT NOT NULL REFERENCES revenue_sources(source_id),
    unit_id TEXT NOT NULL,
    energy_kind TEXT NOT NULL,
    reserved_mwh TEXT NOT NULL,
    unit_price_cny TEXT NOT NULL,
    amount_cny TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'reserved'
        CHECK(state IN ('reserved','settled','partially_settled','released','carried','failed','adjusted')),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reservations_source
ON reservations(source_id, state);
CREATE INDEX IF NOT EXISTS idx_reservations_run
ON reservations(run_id, reservation_id);

CREATE TABLE IF NOT EXISTS source_quota_ledger (
    ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES revenue_sources(source_id),
    reservation_id TEXT REFERENCES reservations(reservation_id),
    run_id INTEGER REFERENCES allocation_runs(run_id),
    movement TEXT NOT NULL CHECK(movement IN ('reserve','settle','release','carry_in','carry_out')),
    delta_mwh TEXT NOT NULL,
    balance_mwh TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES revenue_users(user_id),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quota_source
ON source_quota_ledger(source_id, ledger_id);

CREATE TABLE IF NOT EXISTS settlements (
    settlement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES allocation_runs(run_id),
    actual_version_id TEXT REFERENCES metering_versions(metering_version_id),
    actual_mwh TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('full','partial','carry')),
    idempotency_key TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL REFERENCES revenue_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settlement_slices (
    slice_id INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id INTEGER NOT NULL REFERENCES settlements(settlement_id),
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    source_id TEXT NOT NULL,
    settled_mwh TEXT NOT NULL,
    released_mwh TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES allocation_runs(run_id),
    reason TEXT NOT NULL,
    requested_by TEXT NOT NULL REFERENCES revenue_users(user_id),
    reviewed_by TEXT REFERENCES revenue_users(user_id),
    new_run_id INTEGER REFERENCES allocation_runs(run_id),
    state TEXT NOT NULL DEFAULT 'requested' CHECK(state IN ('requested','reviewed','rejected')),
    created_at TEXT NOT NULL,
    CHECK(requested_by <> reviewed_by)
);

CREATE TABLE IF NOT EXISTS adjustment_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    adjustment_id INTEGER NOT NULL REFERENCES manual_adjustments(adjustment_id),
    source_id TEXT,
    line_id TEXT NOT NULL,
    delta_mwh TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revenue_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS revenue_audit_events (
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

CREATE INDEX IF NOT EXISTS idx_revenue_audit_entity
ON revenue_audit_events(entity_type, entity_id, event_id);
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
