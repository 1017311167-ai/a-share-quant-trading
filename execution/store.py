"""订单执行持久化存储。"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import uuid
from pathlib import Path

from execution.models import (
    ExecutionEvent,
    ExecutionStatus,
    ManagedOrder,
    OrderIntent,
)
from broker_adapter.models import TradeSnapshot


TERMINAL_STATUSES = tuple(
    item.value for item in ExecutionStatus if item.terminal
)


class SQLiteExecutionStore:
    """SQLite 订单执行日志，用于幂等和程序重启恢复。"""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS execution_intents (
                    idempotency_key TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS managed_orders (
                    local_order_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    client_order_id TEXT NOT NULL UNIQUE,
                    broker_order_id TEXT,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(intent_id, attempt_no)
                );
                CREATE INDEX IF NOT EXISTS idx_managed_orders_intent
                    ON managed_orders(intent_id);
                CREATE INDEX IF NOT EXISTS idx_managed_orders_status
                    ON managed_orders(status);
                CREATE TABLE IF NOT EXISTS execution_events (
                    event_id TEXT PRIMARY KEY,
                    local_order_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_execution_events_order
                    ON execution_events(local_order_id, created_at);
                CREATE TABLE IF NOT EXISTS execution_fills (
                    trade_id TEXT PRIMARY KEY,
                    local_order_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_execution_fills_order
                    ON execution_fills(local_order_id, created_at);
            """)

    def save_intent(self, intent: OrderIntent,
                    status: str = "created") -> OrderIntent:
        now = dt.datetime.now().isoformat()
        payload = json.dumps(
            intent.to_dict(), ensure_ascii=False, sort_keys=True
        )
        with self._lock, self._conn:
            self._conn.execute("""
                INSERT OR IGNORE INTO execution_intents
                (idempotency_key, intent_id, payload_json, status,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                intent.idempotency_key,
                intent.intent_id,
                payload,
                status,
                now,
                now,
            ))
        stored = self.get_intent(intent.idempotency_key)
        incoming = intent.to_dict()
        existing = stored.to_dict()
        for value in (incoming, existing):
            value.pop("intent_id", None)
        if incoming != existing:
            raise ValueError(
                f"idempotency_key 冲突：{intent.idempotency_key}"
            )
        return stored

    def get_intent(self, idempotency_key: str) -> OrderIntent:
        with self._lock:
            row = self._conn.execute("""
                SELECT payload_json FROM execution_intents
                WHERE idempotency_key = ?
            """, (idempotency_key,)).fetchone()
        if row is None:
            raise KeyError(f"未找到订单意图：{idempotency_key}")
        return OrderIntent.from_dict(json.loads(row["payload_json"]))

    def update_intent_status(self, idempotency_key: str, status: str):
        with self._lock, self._conn:
            self._conn.execute("""
                UPDATE execution_intents
                SET status = ?, updated_at = ?
                WHERE idempotency_key = ?
            """, (status, dt.datetime.now().isoformat(), idempotency_key))

    def create_order(self, order: ManagedOrder) -> ManagedOrder:
        now = dt.datetime.now().isoformat()
        payload = json.dumps(
            order.to_dict(), ensure_ascii=False, sort_keys=True
        )
        with self._lock, self._conn:
            self._conn.execute("""
                INSERT OR IGNORE INTO managed_orders
                (local_order_id, intent_id, idempotency_key, attempt_no,
                 client_order_id, broker_order_id, status, payload_json,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                order.local_order_id,
                order.intent_id,
                order.idempotency_key,
                order.attempt_no,
                order.client_order_id,
                order.broker_order_id,
                order.status.value,
                payload,
                now,
                now,
            ))
        return self.get_order_by_client_id(order.client_order_id)

    def save_order(self, order: ManagedOrder) -> ManagedOrder:
        payload = json.dumps(
            order.to_dict(), ensure_ascii=False, sort_keys=True
        )
        with self._lock, self._conn:
            self._conn.execute("""
                UPDATE managed_orders
                SET broker_order_id = ?, status = ?, payload_json = ?,
                    updated_at = ?
                WHERE local_order_id = ?
            """, (
                order.broker_order_id,
                order.status.value,
                payload,
                dt.datetime.now().isoformat(),
                order.local_order_id,
            ))
        return order

    def get_order(self, local_order_id: str) -> ManagedOrder:
        return self._get_order("local_order_id", local_order_id)

    def get_order_by_client_id(self, client_order_id: str) -> ManagedOrder:
        return self._get_order("client_order_id", client_order_id)

    def get_order_by_broker_id(self, broker_order_id) -> ManagedOrder | None:
        with self._lock:
            row = self._conn.execute("""
                SELECT payload_json FROM managed_orders
                WHERE broker_order_id = ?
                ORDER BY updated_at DESC LIMIT 1
            """, (str(broker_order_id),)).fetchone()
        return ManagedOrder.from_dict(
            json.loads(row["payload_json"])
        ) if row else None

    def active_order_for_intent(self, intent_id: str) -> ManagedOrder | None:
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        with self._lock:
            row = self._conn.execute(f"""
                SELECT payload_json FROM managed_orders
                WHERE intent_id = ? AND status NOT IN ({placeholders})
                ORDER BY attempt_no DESC LIMIT 1
            """, (intent_id, *TERMINAL_STATUSES)).fetchone()
        return ManagedOrder.from_dict(
            json.loads(row["payload_json"])
        ) if row else None

    def orders_for_intent(self, intent_id: str) -> list[ManagedOrder]:
        with self._lock:
            rows = self._conn.execute("""
                SELECT payload_json FROM managed_orders
                WHERE intent_id = ? ORDER BY attempt_no
            """, (intent_id,)).fetchall()
        return [
            ManagedOrder.from_dict(json.loads(row["payload_json"]))
            for row in rows
        ]

    def list_active_orders(self) -> list[ManagedOrder]:
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        with self._lock:
            rows = self._conn.execute(f"""
                SELECT payload_json FROM managed_orders
                WHERE status NOT IN ({placeholders})
                ORDER BY updated_at
            """, TERMINAL_STATUSES).fetchall()
        return [
            ManagedOrder.from_dict(json.loads(row["payload_json"]))
            for row in rows
        ]

    def append_event(self, event: ExecutionEvent):
        with self._lock, self._conn:
            self._conn.execute("""
                INSERT OR IGNORE INTO execution_events
                (event_id, local_order_id, event_type, created_at,
                 payload_json)
                VALUES (?, ?, ?, ?, ?)
            """, (
                event.event_id,
                event.local_order_id,
                event.event_type,
                event.created_at.isoformat(),
                json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True),
            ))

    def list_events(self, local_order_id: str) -> list[ExecutionEvent]:
        with self._lock:
            rows = self._conn.execute("""
                SELECT payload_json FROM execution_events
                WHERE local_order_id = ? ORDER BY created_at
            """, (local_order_id,)).fetchall()
        events = []
        for row in rows:
            data = json.loads(row["payload_json"])
            data["created_at"] = dt.datetime.fromisoformat(
                data["created_at"]
            )
            events.append(ExecutionEvent(**data))
        return events

    def save_fill(self, local_order_id: str,
                  trade: TradeSnapshot) -> bool:
        """保存成交，重复 trade_id 返回 False。"""
        with self._lock, self._conn:
            cursor = self._conn.execute("""
                INSERT OR IGNORE INTO execution_fills
                (trade_id, local_order_id, payload_json, created_at)
                VALUES (?, ?, ?, ?)
            """, (
                trade.trade_id,
                local_order_id,
                json.dumps(
                    trade.to_dict(), ensure_ascii=False, sort_keys=True
                ),
                dt.datetime.now().isoformat(),
            ))
        return cursor.rowcount > 0

    def list_fills(self, local_order_id: str) -> list[TradeSnapshot]:
        with self._lock:
            rows = self._conn.execute("""
                SELECT payload_json FROM execution_fills
                WHERE local_order_id = ? ORDER BY created_at
            """, (local_order_id,)).fetchall()
        values = []
        for row in rows:
            data = json.loads(row["payload_json"])
            values.append(TradeSnapshot.from_dict(data))
        return values

    def close(self):
        with self._lock:
            self._conn.close()

    def _get_order(self, field: str, value) -> ManagedOrder:
        if field not in {"local_order_id", "client_order_id"}:
            raise ValueError(f"不支持的查询字段：{field}")
        with self._lock:
            row = self._conn.execute(f"""
                SELECT payload_json FROM managed_orders WHERE {field} = ?
            """, (str(value),)).fetchone()
        if row is None:
            raise KeyError(f"未找到订单：{value}")
        return ManagedOrder.from_dict(json.loads(row["payload_json"]))


def new_event(order: ManagedOrder, event_type: str,
              from_status=None, to_status=None,
              details=None) -> ExecutionEvent:
    return ExecutionEvent(
        event_id=str(uuid.uuid4()),
        local_order_id=order.local_order_id,
        event_type=event_type,
        from_status=from_status or order.status.value,
        to_status=to_status or order.status.value,
        created_at=dt.datetime.now(),
        details=dict(details or {}),
    )
