"""统一券商接口和 MockBroker 测试。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker_adapter.contract import assert_broker_contract, assert_fill_contract
from broker_adapter.mock_broker import MockBroker
from broker_adapter.models import OrderStatus


def test_mock_broker_contract():
    broker = MockBroker(fill_mode="none")
    assert_broker_contract(broker)
    print("PASS MockBroker base contract")


def test_mock_broker_partial_fill_contract():
    broker = MockBroker(fill_mode="none", initial_cash=1_000_000)
    assert_fill_contract(broker, quantity=200)
    print("PASS MockBroker partial fill contract")


def test_mock_broker_reject_cancel_reconnect():
    events = []
    rejected = MockBroker(fill_mode="reject", on_event=lambda n, d: events.append((n, d)))
    rejected.connect()
    try:
        rejected.order_buy("600519", 10.0, 100)
    except Exception:
        pass
    assert rejected.get_orders()[0].status is OrderStatus.REJECTED

    broker = MockBroker(fill_mode="none", on_event=lambda n, d: events.append((n, d)))
    broker.connect()
    order_id = broker.order_buy("600519", 10.0, 100)
    assert broker.cancel_order(order_id) is True
    assert broker.get_order(order_id).status is OrderStatus.CANCELLED
    broker.simulate_disconnect()
    try:
        broker.get_account_info()
    except Exception:
        pass
    broker.reconnect(max_attempts=1, delay=0)
    assert broker.get_account_info()["资金账号"] == "MOCK-001"
    assert any(name == "order" for name, _ in events)
    assert any(name == "disconnected" for name, _ in events)
    broker.disconnect()
    print("PASS MockBroker reject/cancel/reconnect")


def test_mock_realtime_and_callbacks():
    events = []
    broker = MockBroker(fill_mode="full", on_event=lambda n, d: events.append((n, d)))
    broker.connect()
    quotes = []
    broker.subscribe_realtime(["600519"], lambda symbol, quote: quotes.append((symbol, quote)))
    broker.push_quote("600519", {"lastPrice": 10.5})
    assert quotes == [("600519", {"lastPrice": 10.5})]

    order_id = broker.order_buy("600519", 10.0, 100)
    order = broker.get_order(order_id)
    assert order.status is OrderStatus.FILLED
    assert broker.get_trades(order_id=order_id)
    event_names = {name for name, _ in events}
    assert {"connected", "order", "trade"} <= event_names
    broker.disconnect()
    print("PASS MockBroker realtime/callback")


def run_test():
    for test in (
        test_mock_broker_contract,
        test_mock_broker_partial_fill_contract,
        test_mock_broker_reject_cancel_reconnect,
        test_mock_realtime_and_callbacks,
    ):
        test()
    print("===== broker contract tests passed =====")


if __name__ == "__main__":
    run_test()
