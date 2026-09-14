"""订单执行管理器离线测试。"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker_adapter.base_broker import BrokerOrderUnknownError
from broker_adapter.mock_broker import MockBroker
from broker_adapter.models import OrderSide, OrderStatus
from execution.manager import OrderExecutionManager
from execution.models import (
    ExecutionPolicy,
    ExecutionStatus,
    OrderIntent,
)
from execution.store import SQLiteExecutionStore
from risk.engine import RiskEngine
from risk.models import AccountState, QuoteState, RiskContext, RiskLimits
from trading.session import TradingSessionGuard


class TimeoutOnceBroker(MockBroker):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fail_next_submit = True

    def submit_order(self, request):
        if self.fail_next_submit:
            self.fail_next_submit = False
            raise BrokerOrderUnknownError("simulated submit timeout")
        return super().submit_order(request)


def _intent(key="intent-001", quantity=100, price=10.0,
            side=OrderSide.BUY) -> OrderIntent:
    return OrderIntent(
        idempotency_key=key,
        symbol="600519",
        side=side,
        quantity=quantity,
        limit_price=price,
    )


def _manager(path, broker, policy=None):
    return OrderExecutionManager(
        broker,
        SQLiteExecutionStore(path),
        policy=policy or ExecutionPolicy(),
        session_guard=TradingSessionGuard.always_open(),
    )


def test_duplicate_intent_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        broker = MockBroker(fill_mode="none")
        broker.connect()
        manager = _manager(f"{tmp}/execution.db", broker)
        first = manager.submit_intent(_intent())
        second = manager.submit_intent(_intent())
        assert first.order is not None
        assert second.reused_existing
        assert second.order.local_order_id == first.order.local_order_id
        assert len(broker.get_orders()) == 1
        manager.store.close()
        broker.disconnect()
    print("PASS duplicate intent idempotency")


def test_restart_reconciles_without_resubmit():
    with tempfile.TemporaryDirectory() as tmp:
        path = f"{tmp}/execution.db"
        broker = MockBroker(fill_mode="none")
        broker.connect()
        manager = _manager(path, broker)
        result = manager.submit_intent(_intent())
        local_id = result.order.local_order_id
        broker_id = result.order.broker_order_id
        manager.store.close()

        recovered_store = SQLiteExecutionStore(path)
        recovered = OrderExecutionManager(
            broker,
            recovered_store,
            session_guard=TradingSessionGuard.always_open(),
        )
        orders = recovered.recover()
        assert len(orders) == 1
        assert orders[0].broker_order_id == broker_id
        assert len(broker.get_orders()) == 1
        assert recovered.get_order(local_id).local_order_id == local_id
        recovered_store.close()
        broker.disconnect()
    print("PASS restart reconciliation")


def test_unknown_submit_reconciles_then_retries_once():
    with tempfile.TemporaryDirectory() as tmp:
        broker = TimeoutOnceBroker(fill_mode="none")
        broker.connect()
        policy = ExecutionPolicy(
            max_attempts=2,
            unknown_grace_seconds=1,
            retry_on_unknown=True,
        )
        manager = _manager(f"{tmp}/execution.db", broker, policy)
        result = manager.submit_intent(_intent())
        assert result.order.status is ExecutionStatus.UNKNOWN
        manager.process_order(
            result.order.local_order_id,
            now=result.order.last_event_at + dt.timedelta(seconds=2),
        )
        attempts = manager.get_orders_for_intent(result.order.intent_id)
        assert len(attempts) == 2
        assert attempts[-1].client_order_id.endswith(":2")
        assert len(broker.get_orders()) == 1
        manager.store.close()
        broker.disconnect()
    print("PASS unknown submit safe retry")


def test_partial_fill_and_completion():
    with tempfile.TemporaryDirectory() as tmp:
        broker = MockBroker(
            fill_mode="partial", partial_fill_ratio=0.5
        )
        broker.connect()
        manager = _manager(f"{tmp}/execution.db", broker)
        result = manager.submit_intent(_intent(quantity=200))
        order = manager.get_order(result.order.local_order_id)
        assert order.status is ExecutionStatus.PARTIALLY_FILLED
        assert order.filled_quantity == 100
        broker.fill_order(order.broker_order_id, quantity=100, price=10.0)
        manager.process_order(order.local_order_id)
        completed = manager.get_order(order.local_order_id)
        assert completed.status is ExecutionStatus.FILLED
        assert completed.filled_quantity == 200
        assert len(manager.get_fills(order.local_order_id)) == 2
        manager.store.close()
        broker.disconnect()
    print("PASS partial fill and completion")


def test_timeout_cancel():
    with tempfile.TemporaryDirectory() as tmp:
        broker = MockBroker(fill_mode="none")
        broker.connect()
        policy = ExecutionPolicy(
            chase_after_seconds=100,
            cancel_after_seconds=10,
        )
        manager = _manager(f"{tmp}/execution.db", broker, policy)
        start = dt.datetime.now()
        result = manager.submit_intent(_intent())
        manager.process_order(
            result.order.local_order_id,
            now=start + dt.timedelta(seconds=11),
        )
        pending = manager.get_order(result.order.local_order_id)
        assert pending.status is ExecutionStatus.CANCEL_PENDING
        manager.process_order(pending.local_order_id)
        cancelled = manager.get_order(pending.local_order_id)
        assert cancelled.status is ExecutionStatus.CANCELLED
        manager.store.close()
        broker.disconnect()
    print("PASS timeout cancel")


def test_limited_chase_creates_one_replacement():
    with tempfile.TemporaryDirectory() as tmp:
        broker = MockBroker(fill_mode="none")
        broker.connect()
        policy = ExecutionPolicy(
            max_attempts=2,
            chase_after_seconds=1,
            cancel_after_seconds=60,
            max_chase_steps=1,
            chase_step_ticks=1,
            price_tick=0.01,
            max_price_deviation_pct=0.01,
        )
        manager = _manager(f"{tmp}/execution.db", broker, policy)
        result = manager.submit_intent(_intent(price=10.0))
        manager.process_order(
            result.order.local_order_id,
            market_price=10.5,
            now=dt.datetime.now() + dt.timedelta(seconds=2),
        )
        manager.process_order(result.order.local_order_id)
        attempts = manager.get_orders_for_intent(result.order.intent_id)
        assert len(attempts) == 2
        assert attempts[-1].limit_price > attempts[0].limit_price
        assert len(broker.get_orders()) == 2
        manager.store.close()
        broker.disconnect()
    print("PASS limited chase")


def test_explicit_rejection_retry_policy():
    with tempfile.TemporaryDirectory() as tmp:
        broker = MockBroker(fill_mode="reject")
        broker.connect()
        policy = ExecutionPolicy(
            max_attempts=2,
            retry_on_rejected=True,
        )
        manager = _manager(f"{tmp}/execution.db", broker, policy)
        result = manager.submit_intent(_intent())
        attempts = manager.get_orders_for_intent(result.order.intent_id)
        assert len(attempts) == 2
        assert all(item.status is ExecutionStatus.REJECTED for item in attempts)
        manager.store.close()
        broker.disconnect()
    print("PASS explicit rejection retry")


def test_execution_manager_uses_risk_engine():
    with tempfile.TemporaryDirectory() as tmp:
        broker = MockBroker(fill_mode="none", initial_cash=1_000_000)
        broker.connect()
        quote = QuoteState(
            symbol="600519",
            last_price=10.0,
            previous_close=10.0,
            limit_up=11.0,
            limit_down=9.0,
        )
        context = RiskContext(
            account=AccountState(
                total_asset=1_000_000,
                available_cash=1_000_000,
            ),
            quotes={"600519": quote},
            now=dt.datetime(2026, 9, 14, 10, 0, 0),
        )
        risk = RiskEngine(RiskLimits(
            max_single_position_pct=0.2,
            max_gross_exposure_pct=0.8,
            min_cash_pct=0.05,
        ))
        manager = OrderExecutionManager(
            broker,
            SQLiteExecutionStore(f"{tmp}/execution.db"),
            risk_engine=risk,
            risk_context_provider=lambda: context,
            session_guard=TradingSessionGuard.always_open(),
        )
        result = manager.submit_intent(_intent(quantity=30_000))
        assert result.risk_decision.resized
        assert result.order.requested_quantity == 20_000
        assert broker.get_order(result.order.broker_order_id).quantity == 20_000
        manager.store.close()
        broker.disconnect()
    print("PASS execution manager risk integration")


def run_test():
    for test in (
        test_duplicate_intent_is_idempotent,
        test_restart_reconciles_without_resubmit,
        test_unknown_submit_reconciles_then_retries_once,
        test_partial_fill_and_completion,
        test_timeout_cancel,
        test_limited_chase_creates_one_replacement,
        test_explicit_rejection_retry_policy,
        test_execution_manager_uses_risk_engine,
    ):
        test()
    print("===== order execution tests passed =====")


if __name__ == "__main__":
    run_test()
