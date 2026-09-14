"""SQLite 数据库连接与建表。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


SCHEMA_VERSION = "trading-persistence-v1"


class SQLiteDatabase:
    """线程安全的 SQLite 数据库封装。"""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        self._initialize()

    @contextmanager
    def transaction(self):
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def execute(self, sql, params=()):
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    def executemany(self, sql, rows):
        with self._lock, self._conn:
            return self._conn.executemany(sql, rows)

    def query(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self):
        with self._lock:
            self._conn.close()

    def _initialize(self):
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS schema_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS strategy_instances (
                    strategy_instance_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    strategy_key TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS signals (
                    signal_id TEXT PRIMARY KEY,
                    strategy_instance_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    signal_time TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_signals_strategy_time
                    ON signals(strategy_instance_id, signal_time);

                CREATE TABLE IF NOT EXISTS order_intents (
                    order_intent_id TEXT PRIMARY KEY,
                    signal_id TEXT,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    requested_quantity INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_order_intents_account_status
                    ON order_intents(account_id, status);

                CREATE TABLE IF NOT EXISTS orders (
                    local_order_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL,
                    account_id TEXT,
                    broker_order_id TEXT,
                    client_order_id TEXT UNIQUE,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_quantity INTEGER NOT NULL,
                    filled_quantity INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_orders_account_status
                    ON orders(account_id, status);
                CREATE INDEX IF NOT EXISTS idx_orders_broker_order
                    ON orders(broker_order_id);

                CREATE TABLE IF NOT EXISTS fills (
                    fill_id TEXT PRIMARY KEY,
                    broker_trade_id TEXT UNIQUE,
                    local_order_id TEXT,
                    broker_order_id TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    filled_quantity INTEGER NOT NULL,
                    fill_price REAL NOT NULL,
                    total_fee REAL NOT NULL DEFAULT 0,
                    filled_at TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_fills_order
                    ON fills(local_order_id, filled_at);

                CREATE TABLE IF NOT EXISTS positions_current (
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    total_quantity INTEGER NOT NULL,
                    available_quantity INTEGER NOT NULL,
                    frozen_quantity INTEGER NOT NULL DEFAULT 0,
                    average_cost REAL NOT NULL DEFAULT 0,
                    market_price REAL NOT NULL DEFAULT 0,
                    market_value REAL NOT NULL DEFAULT 0,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(account_id, symbol)
                );

                CREATE TABLE IF NOT EXISTS position_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    snapshot_time TEXT NOT NULL,
                    total_quantity INTEGER NOT NULL,
                    available_quantity INTEGER NOT NULL,
                    frozen_quantity INTEGER NOT NULL,
                    market_value REAL NOT NULL,
                    source TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_position_snapshots_account_time
                    ON position_snapshots(account_id, snapshot_time);

                CREATE TABLE IF NOT EXISTS cash_snapshots (
                    cash_snapshot_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    snapshot_time TEXT NOT NULL,
                    total_asset REAL NOT NULL,
                    available_cash REAL NOT NULL,
                    frozen_cash REAL NOT NULL,
                    market_value REAL NOT NULL,
                    receivable REAL NOT NULL DEFAULT 0,
                    payable REAL NOT NULL DEFAULT 0,
                    source TEXT NOT NULL,
                    reconciliation_run_id TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cash_snapshots_account_time
                    ON cash_snapshots(account_id, snapshot_time);

                CREATE TABLE IF NOT EXISTS cash_current (
                    account_id TEXT PRIMARY KEY,
                    total_asset REAL NOT NULL,
                    available_cash REAL NOT NULL,
                    frozen_cash REAL NOT NULL DEFAULT 0,
                    market_value REAL NOT NULL DEFAULT 0,
                    receivable REAL NOT NULL DEFAULT 0,
                    payable REAL NOT NULL DEFAULT 0,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS config_versions (
                    config_name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    activated_at TEXT,
                    PRIMARY KEY(config_name, version)
                );

                CREATE TABLE IF NOT EXISTS risk_events (
                    risk_event_id TEXT PRIMARY KEY,
                    account_id TEXT,
                    strategy_instance_id TEXT,
                    order_intent_id TEXT,
                    symbol TEXT,
                    rule_code TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    resolved_at TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_risk_events_account_time
                    ON risk_events(account_id, occurred_at);

                CREATE TABLE IF NOT EXISTS risk_states (
                    account_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    state_reason TEXT NOT NULL DEFAULT '',
                    connected INTEGER NOT NULL DEFAULT 1,
                    policy_hash TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reconciliation_runs (
                    reconciliation_run_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    trade_date TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    difference_count INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reconciliation_account_time
                    ON reconciliation_runs(account_id, started_at);

                CREATE TABLE IF NOT EXISTS reconciliation_differences (
                    difference_id TEXT PRIMARY KEY,
                    reconciliation_run_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    difference_type TEXT NOT NULL,
                    symbol TEXT,
                    local_value TEXT,
                    broker_value TEXT,
                    difference_value TEXT,
                    status TEXT NOT NULL,
                    resolution TEXT,
                    resolved_by TEXT,
                    resolved_at TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reconciliation_diff_run
                    ON reconciliation_differences(reconciliation_run_id, status);

                CREATE TABLE IF NOT EXISTS recovery_cases (
                    recovery_case_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    checks_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ready_at TEXT,
                    confirmed_at TEXT,
                    confirmed_by TEXT,
                    confirmation_note TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_recovery_account_status
                    ON recovery_cases(account_id, status);

                CREATE TABLE IF NOT EXISTS audit_events (
                    audit_event_id TEXT PRIMARY KEY,
                    account_id TEXT,
                    aggregate_type TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_aggregate
                    ON audit_events(aggregate_type, aggregate_id, created_at);
            """)
            # 兼容持久化草稿早期创建、尚未包含 trade_date 的数据库。
            self._ensure_column(
                "reconciliation_runs", "trade_date", "TEXT"
            )
            self._conn.execute("""
                INSERT OR REPLACE INTO schema_metadata(key, value)
                VALUES ('schema_version', ?)
            """, (SCHEMA_VERSION,))

    def _ensure_column(self, table: str, column: str, definition: str):
        columns = {
            row["name"]
            for row in self._conn.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            self._conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )
