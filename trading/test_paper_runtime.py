"""QMT 模拟盘完整链路、回放和故障注入测试。"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker_adapter.mock_broker import MockBroker
from broker_adapter.models import OrderStatus
from data.trading_calendar import TradingCalendar
from execution import ExecutionPolicy, OrderExecutionManager, SQLiteExecutionStore
from persistence import (
    ExecutionPersistenceBridge,
    ReconciliationService,
    RecoveryService,
    RiskEventPersistenceSink,
    SQLiteDatabase,
    TradingRepository,
)
from risk import RiskEngine, RiskLimits, TradingState
from trading.models import ReplayEvent, TradingSignal
from trading.feed import ListSignalFeed
from trading.replay import ReplayRunner
from trading.runtime import PaperTradingRuntime
from trading.safety import PaperTradingSafetyError, assert_paper_trading
from trading.session import TradingSessionGuard
from trading.verification import verify_paper_link


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value

    def set(self, value):
        self.value = value


def _signal(action="buy", key="signal-001", quantity=100, price=10.0):
    return TradingSignal(
        symbol="600519",
        action=action,
        quantity=quantity,
        limit_price=price,
        strategy_instance_id="double-ma-paper-1",
        idempotency_key=key,
        signal_time=dt.datetime(2026, 9, 14, 10, 0, 0),
        data_version="replay-v1",
    )


def _build_runtime(tmp, *, fill_mode="none", clock=None):
    path = f"{tmp}/paper.db"
    clock = clock or MutableClock(dt.datetime(2026, 9, 14, 10, 0, 0))
    broker = MockBroker(
        fill_mode=fill_mode,
        initial_cash=1_000_000.0,
        t_plus_one=True,
    )
    broker.connect()
    repository = TradingRepository(SQLiteDatabase(path))
    execution_store = SQLiteExecutionStore(path)
    risk = RiskEngine(RiskLimits(
        max_single_position_pct=0.20,
        max_gross_exposure_pct=0.60,
        min_cash_pct=0.05,
        max_daily_loss_pct=0.02,
        max_drawdown_pct=0.05,
        max_quote_age_seconds=60,
        max_orders_per_day=20,
        latest_entry_time="14:50",
    ))
    risk.event_sink = RiskEventPersistenceSink(
        repository,
        account_id=broker.account_id,
        engine=lambda: risk,
    )
    session_guard = TradingSessionGuard(
        calendar=TradingCalendar(
            [clock().date()],
            source="test-calendar",
        ),
        clock=clock,
    )
    manager = OrderExecutionManager(
        broker,
        execution_store,
        policy=ExecutionPolicy(),
        risk_engine=risk,
        require_risk=True,
        clock=clock,
        session_guard=session_guard,
    )
    bridge = ExecutionPersistenceBridge(
        repository,
        account_id=broker.account_id,
        t_plus_one=True,
    )
    reconciliation = ReconciliationService(
        repository, broker, risk_engine=risk, clock=clock
    )
    recovery = RecoveryService(
        repository,
        broker,
        execution_manager=manager,
        execution_bridge=bridge,
        reconciliation_service=reconciliation,
        risk_engine=risk,
        clock=clock,
    )
    runtime = PaperTradingRuntime(
        repository=repository,
        broker=broker,
        account_id=broker.account_id,
        risk_engine=risk,
        execution_manager=manager,
        execution_bridge=bridge,
        reconciliation_service=reconciliation,
        recovery_service=recovery,
        poll_interval=0.01,
        reconcile_interval=10,
        clock=clock,
        sleeper=lambda _: None,
    )
    runtime.bootstrap_account(
        operator="paper-test",
        note="测试账户基线",
    )
    case = runtime.start_recovery("paper runtime test")
    assert case["status"] == "ready_for_confirmation"
    runtime.confirm_recovery(
        confirmed_by="paper-test",
        note="确认模拟账户基线",
    )
    runtime.on_quote("600519", {
        "lastPrice": 10.0,
        "lastClose": 10.0,
        "limitUp": 11.0,
        "limitDown": 9.0,
    })
    return (
        runtime, broker, repository, execution_store, risk, manager, clock
    )


def _close(resources):
    runtime, broker, repository, execution_store, *_ = resources
    execution_store.close()
    repository.close()
    broker.disconnect()


def test_full_paper_lifecycle_signal_fill_account_and_reconciliation():
    with tempfile.TemporaryDirectory() as tmp:
        resources = _build_runtime(tmp, fill_mode="full")
        runtime, broker, repository, _, risk, manager, _ = resources
        result = runtime.handle_signal(_signal())
        order = manager.get_order(result.order.local_order_id)
        assert order.status.value == "filled"
        assert len(broker.get_trades()) == 1
        assert repository.get_order(order.local_order_id)["status"] == "filled"
        assert repository.get_signal_by_idempotency_key("signal-001")[
            "status"
        ] == "converted"
        position = repository.get_position_current("MOCK-001", "600519")
        assert position["total_quantity"] == 100
        assert position["available_quantity"] == 0
        cash = repository.get_cash_current("MOCK-001")
        assert cash["available_cash"] == broker.get_account_info()["可用资金"]
        reconciliation = runtime.reconciliation_service.reconcile("MOCK-001")
        assert reconciliation.status == "matched"
        verification = verify_paper_link(
            repository,
            broker,
            "MOCK-001",
            reconciliation_service=runtime.reconciliation_service,
        )
        assert verification.passed
        assert verification.checks["fill_count"] == 1
        assert risk.state is TradingState.ACTIVE
        _close(resources)
    print("PASS full paper lifecycle signal/fill/account/reconciliation")


def test_continuous_loop_consumes_signal_and_polls():
    with tempfile.TemporaryDirectory() as tmp:
        resources = _build_runtime(tmp, fill_mode="full")
        runtime, _, _, _, _, manager, _ = resources
        runtime.signal_feed = ListSignalFeed([
            _signal(key="continuous-001")
        ])
        cycles = runtime.run_forever(max_cycles=1)
        assert cycles == 1
        orders = manager.list_orders()
        assert len(orders) == 1
        assert orders[0].filled_quantity == 100
        _close(resources)
    print("PASS continuous loop consumes signal and polls")


def test_cancel_order_and_partial_fill_verification():
    with tempfile.TemporaryDirectory() as tmp:
        resources = _build_runtime(tmp, fill_mode="none")
        runtime, broker, repository, _, _, manager, _ = resources
        result = runtime.handle_signal(_signal(key="cancel-001"))
        local_order_id = result.order.local_order_id
        broker_order_id = result.order.broker_order_id
        cancelled = runtime.cancel_active_order(local_order_id)
        assert cancelled.status.value == "cancelled"
        assert broker.get_order(broker_order_id).status is OrderStatus.CANCELLED
        assert repository.get_order(local_order_id)["status"] == "cancelled"

        partial = runtime.handle_signal(_signal(
            key="partial-001", quantity=200
        ))
        broker.fill_order(
            partial.order.broker_order_id, quantity=100, price=10.0
        )
        manager.process_order(partial.order.local_order_id)
        runtime.execution_bridge.sync_manager(manager)
        assert manager.get_order(partial.order.local_order_id).filled_quantity == 100
        assert len(repository.list_fills()) == 1
        _close(resources)
    print("PASS cancel and partial-fill verification")


def test_duplicate_signal_and_duplicate_fill_are_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        resources = _build_runtime(tmp, fill_mode="full")
        runtime, broker, repository, _, _, manager, _ = resources
        signal = _signal(key="duplicate-001")
        first = runtime.handle_signal(signal)
        second = runtime.handle_signal(signal)
        assert second.reused_existing
        assert second.order.local_order_id == first.order.local_order_id
        assert len(broker.get_orders()) == 1
        before_position = repository.get_position_current(
            "MOCK-001", "600519"
        )
        runtime.execution_bridge.sync_manager(manager)
        runtime.execution_bridge.sync_manager(manager)
        after_position = repository.get_position_current(
            "MOCK-001", "600519"
        )
        assert after_position["total_quantity"] == before_position[
            "total_quantity"
        ] == 100
        assert len(repository.list_fills()) == 1
        _close(resources)
    print("PASS duplicate signal and duplicate fill idempotency")


def test_replay_disconnect_reconnect_and_manual_reconfirmation():
    with tempfile.TemporaryDirectory() as tmp:
        resources = _build_runtime(tmp, fill_mode="full")
        runtime, _, _, _, risk, _, _ = resources
        replay = ReplayRunner(runtime)
        events = [
            ReplayEvent(
                "signal",
                dt.datetime(2026, 9, 14, 10, 1),
                {"signal": _signal(key="replay-001")},
            ),
            ReplayEvent(
                "disconnect",
                dt.datetime(2026, 9, 14, 10, 2),
                {"message": "replay disconnect"},
            ),
            ReplayEvent(
                "reconnect",
                dt.datetime(2026, 9, 14, 10, 3),
                {"max_attempts": 1},
            ),
        ]
        result = replay.run(events)
        assert not result.errors
        assert risk.state is TradingState.STOP_OPEN
        assert runtime.state == "ready_for_confirmation"
        blocked_cycle = runtime.process_once()
        assert blocked_cycle["skipped"]
        runtime.confirm_recovery(
            confirmed_by="paper-test",
            note="断线重连对账后再次人工确认",
        )
        assert risk.state is TradingState.ACTIVE
        _close(resources)
    print("PASS replay disconnect/reconnect/manual reconfirmation")


def test_runtime_commands_and_heartbeat():
    with tempfile.TemporaryDirectory() as tmp:
        resources = _build_runtime(tmp, fill_mode="none")
        runtime, broker, repository, _, risk, _, _ = resources
        order = runtime.handle_signal(_signal(key="command-order"))
        command = repository.request_runtime_command(
            account_id="MOCK-001",
            command_type="kill_switch",
            requested_by="ui-test",
            payload={"reason": "交易台急停测试"},
        )
        cycle = runtime.process_once()
        assert cycle["skipped"]
        assert cycle["commands"][0]["command_id"] == command["command_id"]
        assert cycle["commands"][0]["status"] == "completed"
        assert risk.state is TradingState.KILLED
        assert runtime.state == "blocked"
        assert broker.get_order(order.order.broker_order_id).status is (
            OrderStatus.CANCELLED
        )
        heartbeat = repository.get_runtime_heartbeat("MOCK-001")
        assert heartbeat["status"] == "blocked"
        assert heartbeat["environment"] == "paper"
        assert heartbeat["payload"]["risk_state"] == "killed"
        _close(resources)
    print("PASS runtime command queue and heartbeat")


def test_risk_gate_cannot_be_bypassed():
    with tempfile.TemporaryDirectory() as tmp:
        resources = _build_runtime(tmp, fill_mode="none")
        runtime, broker, repository, execution_store, risk, manager, clock = resources
        no_risk_manager = OrderExecutionManager(
            broker,
            SQLiteExecutionStore(f"{tmp}/no-risk.db"),
            require_risk=True,
            session_guard=TradingSessionGuard.always_open(),
        )
        try:
            from execution.models import OrderIntent

            no_risk_manager.submit_intent(OrderIntent(
                idempotency_key="risk-bypass-test",
                symbol="600519",
                side="buy",
                quantity=100,
                limit_price=10.0,
            ))
            raise AssertionError("无 RiskEngine 的执行通道必须拒绝下单")
        except RuntimeError:
            pass

        runtime.quotes.clear()
        missing_quote = runtime.handle_signal(_signal(key="no-quote"))
        assert missing_quote.order is None
        assert "MISSING_MARKET_DATA" in missing_quote.risk_decision.rule_codes
        assert len(broker.get_orders()) == 0

        clock.set(dt.datetime(2026, 9, 14, 14, 51))
        runtime.on_quote("600519", {
            "lastPrice": 10.0,
            "lastClose": 10.0,
            "limitUp": 11.0,
            "limitDown": 9.0,
        })
        too_late = runtime.handle_signal(_signal(key="late-entry"))
        assert too_late.order is None
        assert "ENTRY_TIME_LIMIT" in too_late.risk_decision.rule_codes
        assert len(broker.get_orders()) == 0
        no_risk_manager.store.close()
        _close(resources)
    print("PASS risk gate cannot be bypassed")


def test_real_money_modes_are_hard_blocked():
    broker = MockBroker()
    broker.is_simulation = False
    try:
        assert_paper_trading(broker)
        raise AssertionError("非模拟 Broker 必须被拒绝")
    except PaperTradingSafetyError:
        pass
    with mock.patch.dict(os.environ, {"TRADING_STAGE": "real"}, clear=False):
        try:
            assert_paper_trading(MockBroker())
            raise AssertionError("实盘阶段必须被拒绝")
        except PaperTradingSafetyError:
            pass
    with mock.patch.dict(
            os.environ, {"QMT_ALLOW_REAL_TRADING": "true"}, clear=False
    ):
        try:
            assert_paper_trading(MockBroker())
            raise AssertionError("真实交易开关必须被拒绝")
        except PaperTradingSafetyError:
            pass
    print("PASS real-money modes are hard blocked")


def run_test():
    for test in (
        test_full_paper_lifecycle_signal_fill_account_and_reconciliation,
        test_continuous_loop_consumes_signal_and_polls,
        test_cancel_order_and_partial_fill_verification,
        test_duplicate_signal_and_duplicate_fill_are_idempotent,
        test_replay_disconnect_reconnect_and_manual_reconfirmation,
        test_runtime_commands_and_heartbeat,
        test_risk_gate_cannot_be_bypassed,
        test_real_money_modes_are_hard_blocked,
    ):
        test()
    print("===== paper runtime tests passed =====")


if __name__ == "__main__":
    run_test()
