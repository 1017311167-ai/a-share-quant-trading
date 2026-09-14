"""统一券商接口契约测试工具。"""

from __future__ import annotations

from broker_adapter.models import (
    OrderRequest,
    OrderSide,
    OrderStatus,
)


def assert_broker_contract(
        broker,
        *,
        symbol="600519",
        price=10.0,
        quantity=100,
        client_order_id="contract-order-001",
        cancel=True,
):
    """验证适配器是否满足统一交易接口最低契约。"""
    broker.connect()
    assert broker.get_account_info()
    assert isinstance(broker.get_positions(), list)
    assert isinstance(broker.get_orders(), list)
    assert isinstance(broker.get_trades(), list)

    order_id = broker.submit_order(OrderRequest(
        symbol=symbol,
        side=OrderSide.BUY,
        price=price,
        quantity=quantity,
        client_order_id=client_order_id,
    ))
    assert order_id is not None
    order = broker.get_order(order_id)
    assert order.broker_order_id == str(order_id)
    assert order.symbol == symbol
    assert order.quantity == quantity
    assert order.client_order_id == client_order_id
    assert order.status is not OrderStatus.UNKNOWN

    orders = broker.get_orders(symbol=symbol)
    assert any(item.broker_order_id == str(order_id) for item in orders)
    detail = broker.get_order_detail(order_id)
    assert detail.order.broker_order_id == str(order_id)
    assert isinstance(detail.trades, tuple)

    if cancel and not order.status.terminal:
        assert broker.cancel_order(order_id) is True
        cancelled = broker.get_order(order_id)
        assert cancelled.status is OrderStatus.CANCELLED

    broker.disconnect()
    broker.reconnect(max_attempts=1, delay=0)
    assert broker.get_account_info()
    broker.disconnect()


def assert_fill_contract(broker, *, symbol="600519", price=10.0,
                         quantity=200):
    """验证部分成交、全部成交和订单详情。"""
    broker.connect()
    order_id = broker.submit_order(OrderRequest(
        symbol=symbol,
        side=OrderSide.BUY,
        price=price,
        quantity=quantity,
        client_order_id="contract-fill-001",
    ))
    first_quantity = max(100, quantity // 2)
    first_trade = broker.fill_order(
        order_id, quantity=first_quantity, price=price
    )
    partial = broker.get_order(order_id)
    assert partial.status is OrderStatus.PARTIALLY_FILLED
    assert partial.filled_quantity == first_quantity
    assert partial.remaining_quantity == quantity - first_quantity

    if partial.remaining_quantity:
        broker.fill_order(
            order_id,
            quantity=partial.remaining_quantity,
            price=price,
        )
    completed = broker.get_order(order_id)
    assert completed.status is OrderStatus.FILLED
    assert completed.filled_quantity == quantity

    trades = broker.get_trades(order_id=order_id)
    assert len(trades) >= 2
    detail = broker.get_order_detail(order_id)
    assert len(detail.trades) == len(trades)
    assert detail.trades[0].trade_id
    assert first_trade.trade_id == detail.trades[0].trade_id
    broker.disconnect()
