"""统一交易时段闸门测试。"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker_adapter.mock_broker import MockBroker
from broker_adapter.models import OrderSide, OrderStatus
from data.trading_calendar import TradingCalendar
from execution import OrderExecutionManager, OrderIntent, SQLiteExecutionStore
from risk import RiskEngine, execute_kill_switch
from trading.session import TradingSessionClosed, TradingSessionGuard


TRADE_DAY = dt.date(2026, 9, 14)


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value

    def set(self, value):
        self.value = value


def _guard(clock):
    return TradingSessionGuard(
        calendar=TradingCalendar([TRADE_DAY], source="test"),
        clock=clock,
    )


def test_session_windows_lunch_weekend_and_holiday():
    clock = Clock(dt.datetime(2026, 9, 14, 9, 29))
    guard = _guard(clock)
    assert not guard.check().allowed
    clock.set(dt.datetime(2026, 9, 14, 9, 30))
    assert guard.check().session == "morning"
    clock.set(dt.datetime(2026, 9, 14, 11, 30))
    assert guard.check().allowed
    clock.set(dt.datetime(2026, 9, 14, 12, 0))
    assert not guard.check().allowed
    clock.set(dt.datetime(2026, 9, 14, 13, 0))
    assert guard.check().session == "afternoon"
    clock.set(dt.datetime(2026, 9, 14, 15, 0))
    assert guard.check().allowed
    clock.set(dt.datetime(2026, 9, 14, 15, 1))
    assert not guard.check().allowed
    clock.set(dt.datetime(2026, 9, 13, 10, 0))
    assert not guard.check().allowed
    clock.set(dt.datetime(2026, 9, 15, 10, 0))
    assert not guard.check().allowed
    print("PASS session windows, lunch, weekend and holiday")


def _intent(side=OrderSide.BUY, key="session-order"):
    return OrderIntent(
        idempotency_key=key,
        symbol="600519",
        side=side,
        quantity=100,
        limit_price=10.0,
    )


def test_manager_blocks_order_cancel_and_recovery_outside_session():
    with tempfile.TemporaryDirectory() as tmp:
        path = f"{tmp}/session.db"
        clock = Clock(dt.datetime(2026, 9, 14, 10, 0))
        broker = MockBroker(fill_mode="none")
        broker.connect()
        store = SQLiteExecutionStore(path)
        manager = OrderExecutionManager(
            broker,
            store,
            clock=clock,
            session_guard=_guard(clock),
        )
        order = manager.submit_intent(_intent()).order
        clock.set(dt.datetime(2026, 9, 14, 20, 0))
        for side in (OrderSide.BUY, OrderSide.SELL):
            try:
                manager.submit_intent(_intent(
                    side=side, key=f"night-{side.value}"
                ))
                raise AssertionError("夜间买卖委托必须被拦截")
            except TradingSessionClosed:
                pass
        blocked_cancel = manager.cancel_order(order.local_order_id)
        assert not blocked_cancel.is_terminal
        assert broker.get_order(order.broker_order_id).status is OrderStatus.SUBMITTED
        assert any(
            item.event_type == "CANCEL_BLOCKED_BY_SESSION"
            for item in manager.get_events(order.local_order_id)
        )
        store.close()

        recovered_store = SQLiteExecutionStore(path)
        recovered = OrderExecutionManager(
            broker,
            recovered_store,
            clock=clock,
            session_guard=_guard(clock),
        )
        orders = recovered.recover()
        assert len(orders) == 1
        assert len(broker.get_orders()) == 1
        assert any(
            item.event_type == "RECOVERY_BLOCKED_BY_SESSION"
            for item in recovered.get_events(order.local_order_id)
        )
        recovered_store.close()
        broker.disconnect()
    print("PASS order, sell, cancel and recovery session blocking")

def test_emergency_cancel_can_run_outside_session():
    with tempfile.TemporaryDirectory() as tmp:
        clock = Clock(dt.datetime(2026, 9, 14, 10, 0))
        broker = MockBroker(fill_mode="none")
        broker.connect()
        manager = OrderExecutionManager(
            broker,
            SQLiteExecutionStore(f"{tmp}/emergency.db"),
            clock=clock,
            session_guard=_guard(clock),
        )
        order = manager.submit_intent(_intent()).order
        clock.set(dt.datetime(2026, 9, 14, 20, 0))
        cancelled = execute_kill_switch(
            broker,
            RiskEngine(),
            "night emergency",
            session_guard=_guard(clock),
        )
        assert cancelled == 1
        assert broker.get_order(order.broker_order_id).status is OrderStatus.CANCELLED
        manager.store.close()
        broker.disconnect()
    print("PASS emergency cancel outside session")


def run_test():
    for test in (
        test_session_windows_lunch_weekend_and_holiday,
        test_manager_blocks_order_cancel_and_recovery_outside_session,
        test_emergency_cancel_can_run_outside_session,
    ):
        test()
    print("===== trading session guard tests passed =====")


if __name__ == "__main__":
    run_test()
