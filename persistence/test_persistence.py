"""统一持久化、每日对账和人工恢复离线测试。"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker_adapter.mock_broker import MockBroker
from execution import (
    ExecutionPolicy,
    OrderExecutionManager,
    OrderIntent,
    SQLiteExecutionStore,
)
from persistence import (
    ExecutionPersistenceBridge,
    ReconciliationService,
    RecoveryService,
    RiskEventPersistenceSink,
    SQLiteDatabase,
    TradingRepository,
)
from risk import RiskEngine, RiskLimits, TradingState


NOW = dt.datetime(2026, 9, 14, 15, 10, 0)


class FakeNotifier:
    def __init__(self):
        self.alerts = []

    def send_risk_alert(self, message):
        self.alerts.append(dict(message))
        return {"fake": True}


def test_schema_and_transaction_rollback():
    with tempfile.TemporaryDirectory() as tmp:
        db = SQLiteDatabase(f"{tmp}/trading.db")
        tables = {
            row["name"] for row in db.query("""
                SELECT name FROM sqlite_master WHERE type = 'table'
            """)
        }
        required = {
            "strategy_instances",
            "signals",
            "order_intents",
            "orders",
            "fills",
            "positions_current",
            "position_snapshots",
            "cash_current",
            "cash_snapshots",
            "config_versions",
            "risk_events",
            "risk_states",
            "reconciliation_runs",
            "reconciliation_differences",
            "recovery_cases",
            "audit_events",
        }
        assert required <= tables
        try:
            with db.transaction() as conn:
                conn.execute("""
                    INSERT INTO config_versions
                    (config_name, version, active, payload_json, created_at)
                    VALUES ('rollback', 'v1', 1, '{}', ?)
                """, (NOW.isoformat(),))
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass
        assert db.query_one("""
            SELECT * FROM config_versions WHERE config_name = 'rollback'
        """) is None
        db.close()
    print("PASS persistence schema and transaction rollback")


def test_repository_entity_roundtrips_and_idempotency():
    with tempfile.TemporaryDirectory() as tmp:
        repo = TradingRepository(SQLiteDatabase(f"{tmp}/trading.db"))
        strategy_id = "strategy-1"
        repo.save_strategy_instance(
            strategy_instance_id=strategy_id,
            account_id="ACC-1",
            strategy_key="double_ma",
            strategy_version="1.0.0",
            status="running",
            payload={"parameters": {"short": 5, "long": 20}},
        )
        signal = repo.save_signal(
            signal_id="signal-1",
            strategy_instance_id=strategy_id,
            symbol="600519",
            action="buy",
            signal_time=NOW,
            idempotency_key="sig-key-1",
            status="accepted",
            payload={"price": 10.0},
        )
        duplicate = repo.save_signal(
            signal_id="signal-1",
            strategy_instance_id=strategy_id,
            symbol="600519",
            action="buy",
            signal_time=NOW,
            idempotency_key="sig-key-1",
            status="accepted",
            payload={"price": 10.0},
        )
        assert duplicate["signal_id"] == signal["signal_id"]

        intent_id = "intent-1"
        repo.save_order_intent(
            order_intent_id=intent_id,
            signal_id="signal-1",
            account_id="ACC-1",
            symbol="600519",
            side="buy",
            requested_quantity=100,
            status="submitted",
            idempotency_key="intent-key-1",
            payload={"limit_price": 10.0},
        )
        repo.save_order(
            local_order_id="order-1",
            intent_id=intent_id,
            account_id="ACC-1",
            broker_order_id="B-1",
            client_order_id="intent-key-1:1",
            symbol="600519",
            side="buy",
            status="filled",
            requested_quantity=100,
            filled_quantity=100,
            payload={"status": "filled"},
        )
        repo.save_fill(
            fill_id="fill-1",
            broker_trade_id="T-1",
            local_order_id="order-1",
            broker_order_id="B-1",
            symbol="600519",
            side="buy",
            filled_quantity=100,
            fill_price=10.0,
            total_fee=5.0,
            filled_at=NOW,
            payload={"trade_id": "T-1"},
        )
        repo.save_position_snapshot(
            snapshot_id="position-snapshot-1",
            account_id="ACC-1",
            symbol="600519",
            snapshot_time=NOW,
            total_quantity=100,
            available_quantity=100,
            frozen_quantity=0,
            market_value=1000.0,
            source="BROKER",
            payload={"symbol": "600519"},
        )
        repo.save_position_current(
            account_id="ACC-1",
            symbol="600519",
            total_quantity=100,
            available_quantity=100,
            frozen_quantity=0,
            average_cost=10.0,
            market_price=10.0,
            market_value=1000.0,
            source="BROKER",
            payload={"symbol": "600519"},
        )
        repo.save_cash_snapshot(
            cash_snapshot_id="cash-snapshot-1",
            account_id="ACC-1",
            snapshot_time=NOW,
            total_asset=100_000.0,
            available_cash=99_000.0,
            frozen_cash=0,
            market_value=1000.0,
            source="BROKER",
            payload={"account_id": "ACC-1"},
        )
        repo.save_cash_current(
            account_id="ACC-1",
            total_asset=100_000.0,
            available_cash=99_000.0,
            frozen_cash=0,
            market_value=1000.0,
            source="BROKER",
            payload={"account_id": "ACC-1"},
        )
        repo.save_config_version(
            config_name="risk", version="v1", payload={"max": 0.2}
        )
        repo.save_config_version(
            config_name="risk", version="v2", payload={"max": 0.15}
        )
        assert repo.get_active_config("risk")["version"] == "v2"
        repo.save_risk_state(
            account_id="ACC-1",
            state=TradingState.STOP_OPEN,
            state_reason="test",
            payload={"state": "stop_open"},
        )
        repo.append_audit_event(
            audit_event_id="audit-1",
            account_id="ACC-1",
            aggregate_type="account",
            aggregate_id="ACC-1",
            event_type="test",
            payload={"ok": True},
        )
        assert repo.get_strategy_instance(strategy_id)["payload"][
            "parameters"
        ]["short"] == 5
        assert repo.get_order("order-1")["filled_quantity"] == 100
        assert repo.get_fill_by_broker_trade_id("T-1")["fill_price"] == 10.0
        assert repo.get_position_current("ACC-1", "600519")[
            "available_quantity"
        ] == 100
        assert repo.get_cash_current("ACC-1")["available_cash"] == 99_000.0
        assert repo.get_risk_state("ACC-1")["state"] == "stop_open"
        assert repo.get_audit_event("audit-1")["event_type"] == "test"
        repo.close()
    print("PASS repository entity roundtrips and idempotency")


def test_execution_bridge_mirrors_orders_and_fills():
    with tempfile.TemporaryDirectory() as tmp:
        path = f"{tmp}/trading.db"
        broker = MockBroker(fill_mode="none")
        broker.connect()
        execution_store = SQLiteExecutionStore(path)
        manager = OrderExecutionManager(
            broker, execution_store, policy=ExecutionPolicy()
        )
        result = manager.submit_intent(OrderIntent(
            idempotency_key="strategy-1:600519:20260914",
            symbol="600519",
            side="buy",
            quantity=200,
            limit_price=10.0,
            metadata={"strategy_name": "double_ma"},
        ))
        broker.fill_order(result.order.broker_order_id, 100, 10.0)
        manager.process_order(result.order.local_order_id)

        repo = TradingRepository(SQLiteDatabase(path))
        account = broker.get_account_info()
        repo.save_cash_current(
            account_id=broker.account_id,
            total_asset=account["总资产"],
            available_cash=account["可用资金"],
            frozen_cash=account["冻结资金"],
            market_value=account["持仓市值"],
            source="BOOTSTRAP",
            payload=account,
        )
        bridge = ExecutionPersistenceBridge(
            repo, account_id=broker.account_id
        )
        first = bridge.sync_manager(manager)
        second = bridge.sync_manager(manager)
        assert first == second == {"orders": 1, "fills": 1}
        assert repo.get_order(result.order.local_order_id)[
            "filled_quantity"
        ] == 100
        assert len(repo.list_fills()) == 1
        assert len(repo.list_order_intents()) == 1
        repo.close()
        execution_store.close()
        broker.disconnect()
    print("PASS execution bridge mirrors orders and fills")


def test_daily_reconciliation_matches_broker_snapshot():
    with tempfile.TemporaryDirectory() as tmp:
        broker = MockBroker(
            fill_mode="none",
            initial_cash=100_000.0,
            positions={"600519": {"quantity": 100, "cost": 10.0}},
        )
        broker.connect()
        account = broker.get_account_info()
        position = broker.get_positions()[0]
        repo = TradingRepository(SQLiteDatabase(f"{tmp}/trading.db"))
        repo.save_cash_current(
            account_id=broker.account_id,
            total_asset=account["总资产"],
            available_cash=account["可用资金"],
            frozen_cash=account["冻结资金"],
            market_value=account["持仓市值"],
            source="LOCAL",
            payload=account,
        )
        repo.save_position_current(
            account_id=broker.account_id,
            symbol=position["股票代码"],
            total_quantity=position["持仓数量"],
            available_quantity=position["可用数量"],
            frozen_quantity=position["冻结数量"],
            average_cost=position["成本价"],
            market_price=position["最新价"],
            market_value=position["市值"],
            source="LOCAL",
            payload=position,
        )
        risk = RiskEngine()
        service = ReconciliationService(repo, broker, risk_engine=risk)
        result = service.run_daily(
            broker.account_id, trade_date=dt.date(2026, 9, 14)
        )
        assert result.status == "matched"
        assert result.difference_count == 0
        assert risk.state is TradingState.ACTIVE
        repo.close()
        broker.disconnect()
    print("PASS daily reconciliation matches broker snapshot")


def test_reconciliation_mismatch_alerts_and_stops_opening():
    with tempfile.TemporaryDirectory() as tmp:
        broker = MockBroker(fill_mode="none", initial_cash=100_000.0)
        broker.connect()
        repo = TradingRepository(SQLiteDatabase(f"{tmp}/trading.db"))
        notifier = FakeNotifier()
        risk = RiskEngine()
        service = ReconciliationService(
            repo, broker, risk_engine=risk, notifier=notifier
        )
        result = service.reconcile(broker.account_id)
        assert result.status == "mismatched"
        assert any(
            item["difference_type"] == "CASH_MISSING_LOCAL"
            for item in result.differences
        )
        assert notifier.alerts
        assert notifier.alerts[0]["类型"] == "每日账户对账差异"
        assert risk.state is TradingState.STOP_OPEN
        assert repo.list_reconciliation_differences(
            account_id=broker.account_id, open_only=True
        )
        second = service.reconcile(broker.account_id)
        assert second.status == "mismatched"
        assert second.difference_count == result.difference_count
        assert len(notifier.alerts) >= 2
        repo.close()
        broker.disconnect()
    print("PASS reconciliation mismatch alerts and stops opening")


def test_recovery_blocks_then_confirms_after_manual_resolution():
    with tempfile.TemporaryDirectory() as tmp:
        broker = MockBroker(
            fill_mode="none",
            initial_cash=100_000.0,
            positions={"600519": {"quantity": 100, "cost": 10.0}},
        )
        broker.connect()
        repo = TradingRepository(SQLiteDatabase(f"{tmp}/trading.db"))
        risk = RiskEngine()
        reconciliation = ReconciliationService(
            repo, broker, risk_engine=risk
        )
        recovery = RecoveryService(
            repo,
            broker,
            reconciliation_service=reconciliation,
            risk_engine=risk,
        )
        case = recovery.start(broker.account_id, reason="test restart")
        assert case["status"] == "blocked"
        assert "存在未处理账户差异" in case["checks"]["blocking_issues"]

        resolved = reconciliation.resolve_account_with_broker(
            broker.account_id,
            resolved_by="operator@example.com",
            note="已核对券商资金和持仓",
        )
        assert resolved >= 2
        confirmed = recovery.confirm(
            case["recovery_case_id"],
            confirmed_by="operator@example.com",
            note="确认账户状态无误，恢复交易",
        )
        assert confirmed["status"] == "confirmed"
        assert confirmed["confirmed_by"] == "operator@example.com"
        assert risk.state is TradingState.ACTIVE
        assert repo.get_risk_state(broker.account_id)["state"] == "active"
        repo.close()
        broker.disconnect()
    print("PASS recovery blocks then confirms after manual resolution")


def test_crash_recovery_recovers_order_without_duplicate_submit():
    with tempfile.TemporaryDirectory() as tmp:
        path = f"{tmp}/trading.db"
        broker = MockBroker(fill_mode="none", initial_cash=100_000.0)
        broker.connect()
        execution_store = SQLiteExecutionStore(path)
        manager = OrderExecutionManager(
            broker, execution_store, policy=ExecutionPolicy()
        )
        result = manager.submit_intent(OrderIntent(
            idempotency_key="crash-order-1",
            symbol="600519",
            side="buy",
            quantity=100,
            limit_price=10.0,
        ))
        repo = TradingRepository(SQLiteDatabase(path))
        bridge = ExecutionPersistenceBridge(
            repo, account_id=broker.account_id
        )
        bridge.sync_manager(manager)
        account = broker.get_account_info()
        repo.save_cash_current(
            account_id=broker.account_id,
            total_asset=account["总资产"],
            available_cash=account["可用资金"],
            frozen_cash=account["冻结资金"],
            market_value=account["持仓市值"],
            source="LOCAL",
            payload=account,
        )
        risk = RiskEngine()
        reconciliation = ReconciliationService(
            repo, broker, risk_engine=risk
        )
        recovery = RecoveryService(
            repo,
            broker,
            execution_manager=manager,
            execution_bridge=bridge,
            reconciliation_service=reconciliation,
            risk_engine=risk,
        )
        case = recovery.start(broker.account_id, reason="simulated crash")
        assert case["status"] == "ready_for_confirmation"
        assert case["checks"]["active_order_count"] == 1
        confirmed = recovery.confirm(
            case["recovery_case_id"],
            confirmed_by="operator@example.com",
            note="确认挂单状态并恢复",
        )
        assert confirmed["status"] == "confirmed"
        assert len(broker.get_orders()) == 1
        assert manager.get_order(result.order.local_order_id).broker_order_id
        assert risk.state is TradingState.ACTIVE
        repo.close()
        execution_store.close()
        broker.disconnect()
    print("PASS crash recovery recovers order without duplicate submit")


def test_risk_event_sink_persists_events_and_state():
    with tempfile.TemporaryDirectory() as tmp:
        repo = TradingRepository(SQLiteDatabase(f"{tmp}/trading.db"))
        engine = RiskEngine(
            RiskLimits(max_daily_loss_pct=0.01),
            event_sink=None,
        )
        engine.event_sink = RiskEventPersistenceSink(
            repo,
            account_id="ACC-1",
            engine=engine,
        )
        engine.enable_reduce_only("test manual reduction")
        events = repo.list_risk_events("ACC-1")
        assert events
        assert events[0]["rule_code"] == "REDUCE_ONLY"
        assert repo.get_risk_state("ACC-1")["state"] == "reduce_only"

        try:
            RecoveryService(repo, None, risk_engine=engine).confirm(
                "missing-case",
                confirmed_by="",
                note="x",
            )
            raise AssertionError("人工确认必须要求 confirmed_by")
        except ValueError:
            pass
        repo.close()
    print("PASS risk event sink persists events and state")


def run_test():
    for test in (
        test_schema_and_transaction_rollback,
        test_repository_entity_roundtrips_and_idempotency,
        test_execution_bridge_mirrors_orders_and_fills,
        test_daily_reconciliation_matches_broker_snapshot,
        test_reconciliation_mismatch_alerts_and_stops_opening,
        test_recovery_blocks_then_confirms_after_manual_resolution,
        test_crash_recovery_recovers_order_without_duplicate_submit,
        test_risk_event_sink_persists_events_and_state,
    ):
        test()
    print("===== persistence/reconciliation/recovery tests passed =====")


if __name__ == "__main__":
    run_test()
