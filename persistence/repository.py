"""交易系统统一持久化仓储。"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path

from broker_adapter.models import TradeSnapshot
from execution.models import ManagedOrder
from risk.models import RiskEvent, TradingState


TERMINAL_EXECUTION_STATUSES = {
    "filled",
    "cancelled",
    "rejected",
    "expired",
    "failed",
}


class TradingRepository:
    """保存策略、执行、账户、配置、风控和对账状态。

    仓储只负责事务和序列化，不直接调用券商。外部副作用由服务层协调。
    """

    def __init__(self, database, *, clock=None):
        self.db = database
        self.clock = clock or dt.datetime.now

    def save_strategy_instance(
            self,
            *,
            strategy_instance_id: str,
            account_id: str,
            strategy_key: str,
            strategy_version: str,
            status: str,
            payload: dict,
            created_at=None,
            updated_at=None,
    ) -> dict:
        created_at = _timestamp(created_at or self.clock())
        updated_at = _timestamp(updated_at or self.clock())
        with self.db.transaction() as conn:
            conn.execute("""
                INSERT INTO strategy_instances
                (strategy_instance_id, account_id, strategy_key,
                 strategy_version, status, payload_json, created_at,
                 updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy_instance_id) DO UPDATE SET
                    account_id = excluded.account_id,
                    strategy_key = excluded.strategy_key,
                    strategy_version = excluded.strategy_version,
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
            """, (
                strategy_instance_id,
                account_id,
                strategy_key,
                strategy_version,
                status,
                _dump(payload),
                created_at,
                updated_at,
            ))
        return self.get_strategy_instance(strategy_instance_id)

    def get_strategy_instance(self, strategy_instance_id: str) -> dict:
        row = self.db.query_one("""
            SELECT * FROM strategy_instances
            WHERE strategy_instance_id = ?
        """, (strategy_instance_id,))
        if row is None:
            raise KeyError(f"未找到策略实例：{strategy_instance_id}")
        return _decode_row(row, "payload_json")

    def list_strategy_instances(self, account_id=None) -> list[dict]:
        if account_id is None:
            rows = self.db.query("""
                SELECT * FROM strategy_instances ORDER BY created_at
            """)
        else:
            rows = self.db.query("""
                SELECT * FROM strategy_instances
                WHERE account_id = ? ORDER BY created_at
            """, (account_id,))
        return [_decode_row(row, "payload_json") for row in rows]

    def save_signal(
            self,
            *,
            signal_id: str,
            strategy_instance_id: str,
            symbol: str,
            action: str,
            signal_time,
            idempotency_key: str,
            status: str,
            payload: dict,
            created_at=None,
    ) -> dict:
        created_at = _timestamp(created_at or self.clock())
        signal_time = _timestamp(signal_time)
        existing = self.get_signal_by_idempotency_key(idempotency_key)
        if existing is not None:
            _assert_same_identity(
                existing,
                {
                    "signal_id": signal_id,
                    "strategy_instance_id": strategy_instance_id,
                    "symbol": symbol,
                    "action": action,
                    "signal_time": signal_time,
                },
                "信号",
            )
            return existing
        with self.db.transaction() as conn:
            conn.execute("""
                INSERT INTO signals
                (signal_id, strategy_instance_id, symbol, action,
                 signal_time, idempotency_key, status, payload_json,
                 created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal_id,
                strategy_instance_id,
                symbol,
                action,
                signal_time,
                idempotency_key,
                status,
                _dump(payload),
                created_at,
            ))
        return self.get_signal(signal_id)

    def get_signal(self, signal_id: str) -> dict:
        row = self.db.query_one(
            "SELECT * FROM signals WHERE signal_id = ?", (signal_id,)
        )
        if row is None:
            raise KeyError(f"未找到信号：{signal_id}")
        return _decode_row(row, "payload_json")

    def get_signal_by_idempotency_key(
            self, idempotency_key: str) -> dict | None:
        row = self.db.query_one("""
            SELECT * FROM signals WHERE idempotency_key = ?
        """, (idempotency_key,))
        return _decode_row(row, "payload_json") if row else None

    def save_order_intent(
            self,
            *,
            order_intent_id: str,
            account_id: str,
            symbol: str,
            side: str,
            requested_quantity: int,
            status: str,
            idempotency_key: str,
            payload: dict,
            signal_id: str | None = None,
            created_at=None,
            updated_at=None,
    ) -> dict:
        created_at = _timestamp(created_at or self.clock())
        updated_at = _timestamp(updated_at or self.clock())
        with self.db.transaction() as conn:
            conn.execute("""
                INSERT INTO order_intents
                (order_intent_id, signal_id, account_id, symbol, side,
                 requested_quantity, status, idempotency_key,
                 payload_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_intent_id) DO UPDATE SET
                    signal_id = excluded.signal_id,
                    account_id = excluded.account_id,
                    symbol = excluded.symbol,
                    side = excluded.side,
                    requested_quantity = excluded.requested_quantity,
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
            """, (
                order_intent_id,
                signal_id,
                account_id,
                symbol,
                side,
                int(requested_quantity),
                status,
                idempotency_key,
                _dump(payload),
                created_at,
                updated_at,
            ))
        return self.get_order_intent(order_intent_id)

    def get_order_intent(self, order_intent_id: str) -> dict:
        row = self.db.query_one("""
            SELECT * FROM order_intents WHERE order_intent_id = ?
        """, (order_intent_id,))
        if row is None:
            raise KeyError(f"未找到订单意图：{order_intent_id}")
        return _decode_row(row, "payload_json")

    def list_order_intents(self, account_id=None) -> list[dict]:
        if account_id is None:
            rows = self.db.query(
                "SELECT * FROM order_intents ORDER BY created_at"
            )
        else:
            rows = self.db.query("""
                SELECT * FROM order_intents
                WHERE account_id = ? ORDER BY created_at
            """, (account_id,))
        return [_decode_row(row, "payload_json") for row in rows]

    def save_order(
            self,
            *,
            local_order_id: str,
            intent_id: str,
            symbol: str,
            side: str,
            status: str,
            requested_quantity: int,
            payload: dict,
            account_id: str | None = None,
            broker_order_id=None,
            client_order_id: str | None = None,
            filled_quantity: int = 0,
            created_at=None,
            updated_at=None,
    ) -> dict:
        created_at = _timestamp(created_at or self.clock())
        updated_at = _timestamp(updated_at or self.clock())
        with self.db.transaction() as conn:
            existing = conn.execute("""
                SELECT created_at FROM orders WHERE local_order_id = ?
            """, (local_order_id,)).fetchone()
            if existing is not None:
                created_at = existing["created_at"]
            conn.execute("""
                INSERT INTO orders
                (local_order_id, intent_id, account_id, broker_order_id,
                 client_order_id, symbol, side, status,
                 requested_quantity, filled_quantity, payload_json,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(local_order_id) DO UPDATE SET
                    account_id = excluded.account_id,
                    broker_order_id = excluded.broker_order_id,
                    client_order_id = excluded.client_order_id,
                    symbol = excluded.symbol,
                    side = excluded.side,
                    status = excluded.status,
                    requested_quantity = excluded.requested_quantity,
                    filled_quantity = excluded.filled_quantity,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
            """, (
                local_order_id,
                intent_id,
                account_id,
                _optional_text(broker_order_id),
                client_order_id,
                symbol,
                side,
                status,
                int(requested_quantity),
                int(filled_quantity),
                _dump(payload),
                created_at,
                updated_at,
            ))
        return self.get_order(local_order_id)

    def get_order(self, local_order_id: str) -> dict:
        row = self.db.query_one("""
            SELECT * FROM orders WHERE local_order_id = ?
        """, (local_order_id,))
        if row is None:
            raise KeyError(f"未找到订单：{local_order_id}")
        return _decode_row(row, "payload_json")

    def find_order_by_broker_id(self, broker_order_id) -> dict | None:
        row = self.db.query_one("""
            SELECT * FROM orders WHERE broker_order_id = ?
            ORDER BY updated_at DESC LIMIT 1
        """, (_optional_text(broker_order_id),))
        return _decode_row(row, "payload_json") if row else None

    def find_order_by_client_id(self, client_order_id: str) -> dict | None:
        row = self.db.query_one("""
            SELECT * FROM orders WHERE client_order_id = ?
        """, (client_order_id,))
        return _decode_row(row, "payload_json") if row else None

    def list_orders(
            self,
            *,
            account_id=None,
            active_only: bool = False,
    ) -> list[dict]:
        conditions = []
        params = []
        if account_id is not None:
            conditions.append("account_id = ?")
            params.append(account_id)
        if active_only:
            placeholders = ",".join("?" for _ in TERMINAL_EXECUTION_STATUSES)
            conditions.append(f"status NOT IN ({placeholders})")
            params.extend(sorted(TERMINAL_EXECUTION_STATUSES))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.db.query(
            f"SELECT * FROM orders {where} ORDER BY created_at", tuple(params)
        )
        return [_decode_row(row, "payload_json") for row in rows]

    def save_fill(
            self,
            *,
            fill_id: str,
            broker_trade_id: str,
            symbol: str,
            side: str,
            filled_quantity: int,
            fill_price: float,
            payload: dict,
            local_order_id: str | None = None,
            broker_order_id=None,
            total_fee: float = 0,
            filled_at=None,
            created_at=None,
    ) -> dict:
        created_at = _timestamp(created_at or self.clock())
        filled_at = _timestamp(filled_at) if filled_at is not None else None
        existing = self.get_fill_by_broker_trade_id(broker_trade_id)
        if existing is not None:
            _assert_same_identity(
                existing,
                {
                    "fill_id": fill_id,
                    "symbol": symbol,
                    "filled_quantity": int(filled_quantity),
                    "fill_price": float(fill_price),
                },
                "成交",
            )
            return existing
        with self.db.transaction() as conn:
            conn.execute("""
                INSERT INTO fills
                (fill_id, broker_trade_id, local_order_id,
                 broker_order_id, symbol, side, filled_quantity,
                 fill_price, total_fee, filled_at, payload_json,
                 created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                fill_id,
                broker_trade_id,
                local_order_id,
                _optional_text(broker_order_id),
                symbol,
                side,
                int(filled_quantity),
                float(fill_price),
                float(total_fee),
                filled_at,
                _dump(payload),
                created_at,
            ))
        return self.get_fill(fill_id)

    def get_fill(self, fill_id: str) -> dict:
        row = self.db.query_one(
            "SELECT * FROM fills WHERE fill_id = ?", (fill_id,)
        )
        if row is None:
            raise KeyError(f"未找到成交：{fill_id}")
        return _decode_row(row, "payload_json")

    def get_fill_by_broker_trade_id(
            self, broker_trade_id: str) -> dict | None:
        row = self.db.query_one("""
            SELECT * FROM fills WHERE broker_trade_id = ?
        """, (str(broker_trade_id),))
        return _decode_row(row, "payload_json") if row else None

    def list_fills(
            self, *, local_order_id=None, broker_order_id=None) -> list[dict]:
        conditions = []
        params = []
        if local_order_id is not None:
            conditions.append("local_order_id = ?")
            params.append(local_order_id)
        if broker_order_id is not None:
            conditions.append("broker_order_id = ?")
            params.append(_optional_text(broker_order_id))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.db.query(
            f"SELECT * FROM fills {where} ORDER BY filled_at, created_at",
            tuple(params),
        )
        return [_decode_row(row, "payload_json") for row in rows]

    def save_position_snapshot(
            self,
            *,
            snapshot_id: str,
            account_id: str,
            symbol: str,
            snapshot_time,
            total_quantity: int,
            available_quantity: int,
            frozen_quantity: int,
            market_value: float,
            source: str,
            payload: dict,
    ) -> dict:
        with self.db.transaction() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO position_snapshots
                (snapshot_id, account_id, symbol, snapshot_time,
                 total_quantity, available_quantity, frozen_quantity,
                 market_value, source, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                snapshot_id,
                account_id,
                symbol,
                _timestamp(snapshot_time),
                int(total_quantity),
                int(available_quantity),
                int(frozen_quantity),
                float(market_value),
                source,
                _dump(payload),
            ))
        return self.get_position_snapshot(snapshot_id)

    def get_position_snapshot(self, snapshot_id: str) -> dict:
        row = self.db.query_one("""
            SELECT * FROM position_snapshots WHERE snapshot_id = ?
        """, (snapshot_id,))
        if row is None:
            raise KeyError(f"未找到持仓快照：{snapshot_id}")
        return _decode_row(row, "payload_json")

    def save_position_current(
            self,
            *,
            account_id: str,
            symbol: str,
            total_quantity: int,
            available_quantity: int,
            frozen_quantity: int = 0,
            average_cost: float = 0,
            market_price: float = 0,
            market_value: float = 0,
            source: str,
            payload: dict,
            updated_at=None,
    ) -> dict:
        updated_at = _timestamp(updated_at or self.clock())
        with self.db.transaction() as conn:
            conn.execute("""
                INSERT INTO positions_current
                (account_id, symbol, total_quantity, available_quantity,
                 frozen_quantity, average_cost, market_price,
                 market_value, source, updated_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, symbol) DO UPDATE SET
                    total_quantity = excluded.total_quantity,
                    available_quantity = excluded.available_quantity,
                    frozen_quantity = excluded.frozen_quantity,
                    average_cost = excluded.average_cost,
                    market_price = excluded.market_price,
                    market_value = excluded.market_value,
                    source = excluded.source,
                    updated_at = excluded.updated_at,
                    payload_json = excluded.payload_json
            """, (
                account_id,
                symbol,
                int(total_quantity),
                int(available_quantity),
                int(frozen_quantity),
                float(average_cost),
                float(market_price),
                float(market_value),
                source,
                updated_at,
                _dump(payload),
            ))
        return self.get_position_current(account_id, symbol)

    def get_position_current(self, account_id: str, symbol: str) -> dict:
        row = self.db.query_one("""
            SELECT * FROM positions_current
            WHERE account_id = ? AND symbol = ?
        """, (account_id, symbol))
        if row is None:
            raise KeyError(f"未找到当前持仓：{account_id}/{symbol}")
        return _decode_row(row, "payload_json")

    def list_positions_current(self, account_id: str) -> list[dict]:
        rows = self.db.query("""
            SELECT * FROM positions_current
            WHERE account_id = ? ORDER BY symbol
        """, (account_id,))
        return [_decode_row(row, "payload_json") for row in rows]

    def latest_position_snapshots(
            self, account_id: str, source: str = "BROKER") -> list[dict]:
        rows = self.db.query("""
            SELECT * FROM position_snapshots
            WHERE account_id = ? AND source = ?
            ORDER BY snapshot_time DESC
        """, (account_id, source))
        latest = {}
        for row in rows:
            if row["symbol"] not in latest:
                latest[row["symbol"]] = _decode_row(row, "payload_json")
        return list(latest.values())

    def save_cash_snapshot(
            self,
            *,
            cash_snapshot_id: str,
            account_id: str,
            snapshot_time,
            total_asset: float,
            available_cash: float,
            frozen_cash: float,
            market_value: float,
            source: str,
            payload: dict,
            receivable: float = 0,
            payable: float = 0,
            reconciliation_run_id: str | None = None,
    ) -> dict:
        with self.db.transaction() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO cash_snapshots
                (cash_snapshot_id, account_id, snapshot_time, total_asset,
                 available_cash, frozen_cash, market_value, receivable,
                 payable, source, reconciliation_run_id, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                cash_snapshot_id,
                account_id,
                _timestamp(snapshot_time),
                float(total_asset),
                float(available_cash),
                float(frozen_cash),
                float(market_value),
                float(receivable),
                float(payable),
                source,
                reconciliation_run_id,
                _dump(payload),
            ))
        return self.get_cash_snapshot(cash_snapshot_id)

    def get_cash_snapshot(self, cash_snapshot_id: str) -> dict:
        row = self.db.query_one("""
            SELECT * FROM cash_snapshots WHERE cash_snapshot_id = ?
        """, (cash_snapshot_id,))
        if row is None:
            raise KeyError(f"未找到资金快照：{cash_snapshot_id}")
        return _decode_row(row, "payload_json")

    def save_cash_current(
            self,
            *,
            account_id: str,
            total_asset: float,
            available_cash: float,
            frozen_cash: float = 0,
            market_value: float = 0,
            receivable: float = 0,
            payable: float = 0,
            source: str,
            payload: dict,
            updated_at=None,
    ) -> dict:
        updated_at = _timestamp(updated_at or self.clock())
        with self.db.transaction() as conn:
            conn.execute("""
                INSERT INTO cash_current
                (account_id, total_asset, available_cash, frozen_cash,
                 market_value, receivable, payable, source, updated_at,
                 payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    total_asset = excluded.total_asset,
                    available_cash = excluded.available_cash,
                    frozen_cash = excluded.frozen_cash,
                    market_value = excluded.market_value,
                    receivable = excluded.receivable,
                    payable = excluded.payable,
                    source = excluded.source,
                    updated_at = excluded.updated_at,
                    payload_json = excluded.payload_json
            """, (
                account_id,
                float(total_asset),
                float(available_cash),
                float(frozen_cash),
                float(market_value),
                float(receivable),
                float(payable),
                source,
                updated_at,
                _dump(payload),
            ))
        return self.get_cash_current(account_id)

    def get_cash_current(self, account_id: str) -> dict | None:
        row = self.db.query_one("""
            SELECT * FROM cash_current WHERE account_id = ?
        """, (account_id,))
        return _decode_row(row, "payload_json") if row else None

    def save_config_version(
            self,
            *,
            config_name: str,
            version: str,
            payload: dict,
            active: bool = True,
            created_at=None,
            activated_at=None,
    ) -> dict:
        created_at = _timestamp(created_at or self.clock())
        activated_at = (
            _timestamp(activated_at or self.clock()) if active else None
        )
        with self.db.transaction() as conn:
            if active:
                conn.execute("""
                    UPDATE config_versions SET active = 0,
                        activated_at = NULL WHERE config_name = ?
                """, (config_name,))
            conn.execute("""
                INSERT INTO config_versions
                (config_name, version, active, payload_json, created_at,
                 activated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(config_name, version) DO UPDATE SET
                    active = excluded.active,
                    payload_json = excluded.payload_json,
                    activated_at = excluded.activated_at
            """, (
                config_name,
                version,
                1 if active else 0,
                _dump(payload),
                created_at,
                activated_at,
            ))
        return self.get_config_version(config_name, version)

    def get_config_version(self, config_name: str, version: str) -> dict:
        row = self.db.query_one("""
            SELECT * FROM config_versions
            WHERE config_name = ? AND version = ?
        """, (config_name, version))
        if row is None:
            raise KeyError(f"未找到配置版本：{config_name}/{version}")
        return _decode_row(row, "payload_json")

    def get_active_config(self, config_name: str) -> dict | None:
        row = self.db.query_one("""
            SELECT * FROM config_versions
            WHERE config_name = ? AND active = 1
            ORDER BY activated_at DESC LIMIT 1
        """, (config_name,))
        return _decode_row(row, "payload_json") if row else None

    def save_risk_event(
            self,
            event: RiskEvent,
            *,
            account_id: str | None = None,
            strategy_instance_id: str | None = None,
            order_intent_id: str | None = None,
            status: str = "triggered",
            resolved_at=None,
            payload: dict | None = None,
    ) -> dict:
        values = dict(payload or {})
        values.update({
            "message": event.message,
            "measured_value": event.measured_value,
            "threshold_value": event.threshold_value,
            "details": dict(event.details),
        })
        with self.db.transaction() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO risk_events
                (risk_event_id, account_id, strategy_instance_id,
                 order_intent_id, symbol, rule_code, severity, action,
                 status, occurred_at, resolved_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event.event_id,
                account_id,
                strategy_instance_id,
                order_intent_id,
                event.symbol,
                event.rule_code,
                event.level.value,
                event.action,
                status,
                _timestamp(event.occurred_at),
                _timestamp(resolved_at) if resolved_at else None,
                _dump(values),
            ))
        return self.get_risk_event(event.event_id)

    def get_risk_event(self, risk_event_id: str) -> dict:
        row = self.db.query_one("""
            SELECT * FROM risk_events WHERE risk_event_id = ?
        """, (risk_event_id,))
        if row is None:
            raise KeyError(f"未找到风险事件：{risk_event_id}")
        return _decode_row(row, "payload_json")

    def list_risk_events(
            self, account_id=None, *, unresolved_only: bool = False) -> list[dict]:
        conditions = []
        params = []
        if account_id is not None:
            conditions.append("account_id = ?")
            params.append(account_id)
        if unresolved_only:
            conditions.append("resolved_at IS NULL")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.db.query(
            f"SELECT * FROM risk_events {where} ORDER BY occurred_at DESC",
            tuple(params),
        )
        return [_decode_row(row, "payload_json") for row in rows]

    def save_risk_state(
            self,
            *,
            account_id: str,
            state: str,
            state_reason: str = "",
            connected: bool = True,
            policy_hash: str = "",
            payload: dict | None = None,
            updated_at=None,
    ) -> dict:
        if not isinstance(state, TradingState):
            state = TradingState(state)
        updated_at = _timestamp(updated_at or self.clock())
        with self.db.transaction() as conn:
            conn.execute("""
                INSERT INTO risk_states
                (account_id, state, state_reason, connected, policy_hash,
                 payload_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    state = excluded.state,
                    state_reason = excluded.state_reason,
                    connected = excluded.connected,
                    policy_hash = excluded.policy_hash,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
            """, (
                account_id,
                state.value,
                state_reason,
                1 if connected else 0,
                policy_hash,
                _dump(payload or {}),
                updated_at,
            ))
        return self.get_risk_state(account_id)

    def get_risk_state(self, account_id: str) -> dict | None:
        row = self.db.query_one("""
            SELECT * FROM risk_states WHERE account_id = ?
        """, (account_id,))
        return _decode_row(row, "payload_json") if row else None

    def start_reconciliation_run(
            self,
            *,
            reconciliation_run_id: str,
            account_id: str,
            started_at,
            payload: dict | None = None,
            trade_date=None,
    ) -> dict:
        with self.db.transaction() as conn:
            conn.execute("""
                INSERT INTO reconciliation_runs
                (reconciliation_run_id, account_id, trade_date, started_at,
                 status, difference_count, payload_json)
                VALUES (?, ?, ?, ?, 'running', 0, ?)
            """, (
                reconciliation_run_id,
                account_id,
                _date_text(trade_date) if trade_date is not None else None,
                _timestamp(started_at),
                _dump(payload or {}),
            ))
        return self.get_reconciliation_run(reconciliation_run_id)

    def finish_reconciliation_run(
            self,
            reconciliation_run_id: str,
            *,
            status: str,
            difference_count: int,
            finished_at,
            payload: dict | None = None,
    ) -> dict:
        existing = self.get_reconciliation_run(reconciliation_run_id)
        merged = dict(existing.get("payload") or {})
        merged.update(payload or {})
        with self.db.transaction() as conn:
            conn.execute("""
                UPDATE reconciliation_runs
                SET finished_at = ?, status = ?, difference_count = ?,
                    payload_json = ?
                WHERE reconciliation_run_id = ?
            """, (
                _timestamp(finished_at),
                status,
                int(difference_count),
                _dump(merged),
                reconciliation_run_id,
            ))
        return self.get_reconciliation_run(reconciliation_run_id)

    def get_reconciliation_run(self, reconciliation_run_id: str) -> dict:
        row = self.db.query_one("""
            SELECT * FROM reconciliation_runs
            WHERE reconciliation_run_id = ?
        """, (reconciliation_run_id,))
        if row is None:
            raise KeyError(f"未找到对账批次：{reconciliation_run_id}")
        return _decode_row(row, "payload_json")

    def save_reconciliation_difference(
            self,
            *,
            difference_id: str,
            reconciliation_run_id: str,
            account_id: str,
            difference_type: str,
            payload: dict,
            symbol: str | None = None,
            local_value=None,
            broker_value=None,
            difference_value=None,
            status: str = "open",
            created_at=None,
    ) -> dict:
        created_at = _timestamp(created_at or self.clock())
        with self.db.transaction() as conn:
            conn.execute("""
                INSERT INTO reconciliation_differences
                (difference_id, reconciliation_run_id, account_id,
                 difference_type, symbol, local_value, broker_value,
                 difference_value, status, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                difference_id,
                reconciliation_run_id,
                account_id,
                difference_type,
                symbol,
                _value_text(local_value),
                _value_text(broker_value),
                _value_text(difference_value),
                status,
                _dump(payload),
                created_at,
            ))
        return self.get_reconciliation_difference(difference_id)

    def get_reconciliation_difference(self, difference_id: str) -> dict:
        row = self.db.query_one("""
            SELECT * FROM reconciliation_differences
            WHERE difference_id = ?
        """, (difference_id,))
        if row is None:
            raise KeyError(f"未找到对账差异：{difference_id}")
        return _decode_row(row, "payload_json")

    def list_reconciliation_differences(
            self,
            *,
            reconciliation_run_id: str | None = None,
            account_id: str | None = None,
            open_only: bool = False,
    ) -> list[dict]:
        conditions = []
        params = []
        if reconciliation_run_id is not None:
            conditions.append("reconciliation_run_id = ?")
            params.append(reconciliation_run_id)
        if account_id is not None:
            conditions.append("account_id = ?")
            params.append(account_id)
        if open_only:
            conditions.append("status = 'open'")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.db.query(
            f"SELECT * FROM reconciliation_differences {where} "
            "ORDER BY created_at",
            tuple(params),
        )
        return [_decode_row(row, "payload_json") for row in rows]

    def resolve_reconciliation_difference(
            self,
            difference_id: str,
            *,
            resolved_by: str,
            note: str,
            resolution: str,
            resolved_at=None,
    ) -> dict:
        if not str(resolved_by or "").strip():
            raise ValueError("人工处理差异必须提供 resolved_by")
        if not str(note or "").strip():
            raise ValueError("人工处理差异必须提供处理说明")
        resolved_at = _timestamp(resolved_at or self.clock())
        with self.db.transaction() as conn:
            cursor = conn.execute("""
                UPDATE reconciliation_differences
                SET status = 'resolved', resolution = ?, resolved_by = ?,
                    resolved_at = ?
                WHERE difference_id = ? AND status = 'open'
            """, (
                f"{resolution} | {note}",
                resolved_by,
                resolved_at,
                difference_id,
            ))
            if cursor.rowcount == 0:
                existing = conn.execute("""
                    SELECT status FROM reconciliation_differences
                    WHERE difference_id = ?
                """, (difference_id,)).fetchone()
                if existing is None:
                    raise KeyError(f"未找到对账差异：{difference_id}")
                if existing["status"] != "resolved":
                    raise ValueError(f"差异状态不可处理：{existing['status']}")
        return self.get_reconciliation_difference(difference_id)

    def create_recovery_case(
            self,
            *,
            recovery_case_id: str,
            account_id: str,
            status: str,
            reason: str,
            checks: dict,
            started_at,
            payload: dict | None = None,
    ) -> dict:
        with self.db.transaction() as conn:
            conn.execute("""
                INSERT INTO recovery_cases
                (recovery_case_id, account_id, status, reason, checks_json,
                 started_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                recovery_case_id,
                account_id,
                status,
                reason,
                _dump(checks),
                _timestamp(started_at),
                _dump(payload or {}),
            ))
        return self.get_recovery_case(recovery_case_id)

    def update_recovery_case(
            self,
            recovery_case_id: str,
            *,
            status: str,
            checks: dict,
            ready_at=None,
            confirmed_at=None,
            confirmed_by=None,
            confirmation_note=None,
            payload: dict | None = None,
    ) -> dict:
        existing = self.get_recovery_case(recovery_case_id)
        merged = dict(existing.get("payload") or {})
        merged.update(payload or {})
        with self.db.transaction() as conn:
            conn.execute("""
                UPDATE recovery_cases
                SET status = ?, checks_json = ?, ready_at = COALESCE(?, ready_at),
                    confirmed_at = COALESCE(?, confirmed_at),
                    confirmed_by = COALESCE(?, confirmed_by),
                    confirmation_note = COALESCE(?, confirmation_note),
                    payload_json = ?
                WHERE recovery_case_id = ?
            """, (
                status,
                _dump(checks),
                _timestamp(ready_at) if ready_at else None,
                _timestamp(confirmed_at) if confirmed_at else None,
                confirmed_by,
                confirmation_note,
                _dump(merged),
                recovery_case_id,
            ))
        return self.get_recovery_case(recovery_case_id)

    def get_recovery_case(self, recovery_case_id: str) -> dict:
        row = self.db.query_one("""
            SELECT * FROM recovery_cases WHERE recovery_case_id = ?
        """, (recovery_case_id,))
        if row is None:
            raise KeyError(f"未找到恢复记录：{recovery_case_id}")
        return _decode_row(row, "payload_json")

    def append_audit_event(
            self,
            *,
            audit_event_id: str,
            aggregate_type: str,
            aggregate_id: str,
            event_type: str,
            payload: dict,
            account_id: str | None = None,
            created_at=None,
    ) -> dict:
        created_at = _timestamp(created_at or self.clock())
        with self.db.transaction() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO audit_events
                (audit_event_id, account_id, aggregate_type, aggregate_id,
                 event_type, created_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                audit_event_id,
                account_id,
                aggregate_type,
                aggregate_id,
                event_type,
                created_at,
                _dump(payload),
            ))
        return self.get_audit_event(audit_event_id)

    def get_audit_event(self, audit_event_id: str) -> dict:
        row = self.db.query_one("""
            SELECT * FROM audit_events WHERE audit_event_id = ?
        """, (audit_event_id,))
        if row is None:
            raise KeyError(f"未找到审计事件：{audit_event_id}")
        return _decode_row(row, "payload_json")

    def close(self):
        self.db.close()

    @classmethod
    def open(cls, path):
        from persistence.database import SQLiteDatabase

        return cls(SQLiteDatabase(Path(path)))


class ExecutionPersistenceBridge:
    """把执行管理器的订单和成交同步到统一数据库。"""

    def __init__(
            self,
            repository: TradingRepository,
            *,
            account_id: str,
            signal_id_by_intent: dict | None = None,
    ):
        if not account_id:
            raise ValueError("同步执行状态必须提供 account_id")
        self.repository = repository
        self.account_id = account_id
        self.signal_id_by_intent = dict(signal_id_by_intent or {})

    def sync_order(
            self, order: ManagedOrder, *, intent_payload=None) -> dict:
        payload = order.to_dict()
        signal_id = self.signal_id_by_intent.get(order.intent_id)
        self.repository.save_order_intent(
            order_intent_id=order.intent_id,
            account_id=self.account_id,
            symbol=order.symbol,
            side=order.side.value,
            requested_quantity=payload.get("metadata", {}).get(
                "intent_quantity", order.requested_quantity
            ),
            status=order.status.value,
            idempotency_key=order.idempotency_key,
            payload=intent_payload or {"source": "execution_manager"},
            signal_id=signal_id,
            created_at=order.submitted_at or order.last_event_at,
            updated_at=order.last_event_at,
        )
        return self.repository.save_order(
            local_order_id=order.local_order_id,
            intent_id=order.intent_id,
            account_id=self.account_id,
            broker_order_id=order.broker_order_id,
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side.value,
            status=order.status.value,
            requested_quantity=order.requested_quantity,
            filled_quantity=order.filled_quantity,
            payload=payload,
            created_at=order.submitted_at or order.last_event_at,
            updated_at=order.last_event_at,
        )

    def sync_fill(
            self, order: ManagedOrder, trade: TradeSnapshot) -> dict:
        payload = trade.to_dict()
        return self.repository.save_fill(
            fill_id=f"broker:{trade.trade_id}",
            broker_trade_id=trade.trade_id,
            local_order_id=order.local_order_id,
            broker_order_id=trade.broker_order_id,
            symbol=trade.symbol,
            side=trade.side.value,
            filled_quantity=trade.quantity,
            fill_price=trade.price,
            total_fee=trade.total_fee,
            filled_at=trade.traded_at,
            payload=payload,
        )

    def sync_manager(self, manager) -> dict:
        orders = 0
        fills = 0
        for order in manager.store.list_orders():
            try:
                intent = manager.store.get_intent(order.idempotency_key)
                intent_payload = intent.to_dict()
            except KeyError:
                intent_payload = None
            self.sync_order(order, intent_payload=intent_payload)
            orders += 1
            for trade in manager.get_fills(order.local_order_id):
                self.sync_fill(order, trade)
                fills += 1
        return {"orders": orders, "fills": fills}


class RiskEventPersistenceSink:
    """RiskEngine 事件回调：追加事件并保存最新风险状态。"""

    def __init__(
            self,
            repository: TradingRepository,
            *,
            account_id: str,
            strategy_instance_id: str | None = None,
            order_intent_id: str | None = None,
            engine=None,
    ):
        if not account_id:
            raise ValueError("持久化风险事件必须提供 account_id")
        self.repository = repository
        self.account_id = account_id
        self.strategy_instance_id = strategy_instance_id
        self.order_intent_id = order_intent_id
        self.engine = engine

    def __call__(self, event: RiskEvent):
        self.repository.save_risk_event(
            event,
            account_id=self.account_id,
            strategy_instance_id=self.strategy_instance_id,
            order_intent_id=self.order_intent_id,
        )
        engine = self.engine
        if callable(engine):
            engine = engine()
        if engine is not None:
            self.repository.save_risk_state(
                account_id=self.account_id,
                state=engine.state,
                state_reason=engine.state_reason,
                connected=engine.connected,
                policy_hash=engine.policy_hash,
                payload=engine.snapshot(),
            )


def new_id() -> str:
    return str(uuid.uuid4())


def _decode_row(row, payload_key: str | None = None) -> dict:
    result = dict(row)
    if payload_key and payload_key in result:
        result["payload"] = json.loads(result.pop(payload_key))
    if "checks_json" in result:
        result["checks"] = json.loads(result.pop("checks_json"))
    return result


def _dump(value) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True)


def _jsonable(value):
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if hasattr(value, "value") and isinstance(value.value, (str, int, float)):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(value.__dict__)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


def _timestamp(value) -> str:
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time()).isoformat()
    return str(value)


def _date_text(value) -> str:
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def _optional_text(value):
    return None if value is None else str(value)


def _value_text(value):
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple)):
        return _dump(value)
    return str(value)


def _assert_same_identity(existing: dict, incoming: dict, label: str):
    for key, value in incoming.items():
        current = existing.get(key)
        if isinstance(value, float) and isinstance(current, (int, float)):
            if abs(float(current) - value) <= 1e-9:
                continue
        if str(current) != str(value):
            raise ValueError(
                f"{label}幂等键冲突：{key} 已存在 {current!r}，"
                f"新请求为 {value!r}"
            )
