"""实盘风险引擎离线测试。"""

from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker_adapter.mock_broker import MockBroker
from broker_adapter.models import OrderRequest, OrderSide
from risk.engine import (
    RiskEngine,
    execute_kill_switch,
    risk_context_from_broker,
    submit_with_risk,
)
from risk.models import (
    AccountState,
    PositionState,
    QuoteState,
    RiskContext,
    RiskLimits,
    TradingState,
)


NOW = dt.datetime(2026, 9, 14, 10, 0, 0)


def _quote(symbol="600519", price=10.0, *, age=0, tradable=True):
    return QuoteState(
        symbol=symbol,
        last_price=price,
        previous_close=10.0,
        limit_up=11.0,
        limit_down=9.0,
        timestamp=NOW - dt.timedelta(seconds=age),
        tradable=tradable,
    )


def _context(*, cash=1_000_000, total=1_000_000, positions=(),
             quote=None, connected=True, now=NOW):
    return RiskContext(
        account=AccountState(
            total_asset=total,
            available_cash=cash,
            market_value=sum(item.market_value for item in positions),
        ),
        positions=tuple(positions),
        quotes={(quote or _quote()).symbol: quote or _quote()},
        connected=connected,
        now=now,
    )


def _request(side=OrderSide.BUY, quantity=100, price=10.0):
    return OrderRequest(
        symbol="600519",
        side=side,
        price=price,
        quantity=quantity,
        client_order_id="risk-test-001",
    )


def test_pretrade_cash_lot_and_position_limits():
    engine = RiskEngine(RiskLimits(
        max_single_position_pct=0.20,
        max_gross_exposure_pct=0.80,
        min_cash_pct=0.05,
    ))
    decision = engine.pre_trade_check(
        _request(quantity=100), _context()
    )
    assert decision.approved and decision.approved_quantity == 100

    lot_rejected = engine.pre_trade_check(
        _request(quantity=150), _context()
    )
    assert not lot_rejected.approved
    assert "LOT_SIZE" in lot_rejected.rule_codes

    resized = engine.pre_trade_check(
        _request(quantity=30_000), _context()
    )
    assert resized.approved and resized.resized
    assert resized.approved_quantity == 20_000

    cash_resized = engine.pre_trade_check(
        _request(quantity=10_000),
        _context(cash=100_000, total=1_000_000),
    )
    assert cash_resized.approved_quantity == 5_000
    print("PASS cash/lot/single position limits")


def test_t_plus_one_and_price_limits():
    engine = RiskEngine(RiskLimits(lot_size=100))
    locked = PositionState(
        symbol="600519",
        total_quantity=100,
        available_quantity=0,
        market_value=1_000,
    )
    rejected = engine.pre_trade_check(
        _request(OrderSide.SELL, 100),
        _context(positions=(locked,), quote=_quote()),
    )
    assert not rejected.approved
    assert "T_PLUS_1_OR_NO_POSITION" in rejected.rule_codes

    available = PositionState(
        symbol="600519",
        total_quantity=100,
        available_quantity=100,
        market_value=1_000,
    )
    allowed = engine.pre_trade_check(
        _request(OrderSide.SELL, 100),
        _context(positions=(available,), quote=_quote()),
    )
    assert allowed.approved

    price_rejected = engine.pre_trade_check(
        _request(quantity=100, price=12.0),
        _context(quote=_quote()),
    )
    assert not price_rejected.approved
    assert "PRICE_LIMIT_UP" in price_rejected.rule_codes
    print("PASS T+1 and price limits")


def test_gross_exposure_and_min_cash():
    engine = RiskEngine(RiskLimits(
        max_single_position_pct=1.0,
        max_gross_exposure_pct=0.80,
        min_cash_pct=0.05,
    ))
    positions = (
        PositionState(
            symbol="000001",
            total_quantity=70_000,
            available_quantity=70_000,
            market_value=700_000,
        ),
        PositionState(
            symbol="600519",
            total_quantity=0,
            available_quantity=0,
            market_value=0,
        ),
    )
    context = RiskContext(
        account=AccountState(
            total_asset=1_000_000,
            available_cash=300_000,
            market_value=700_000,
        ),
        positions=positions,
        quotes={
            "000001": _quote("000001", 10.0),
            "600519": _quote("600519", 10.0),
        },
        now=NOW,
    )
    decision = engine.pre_trade_check(_request(quantity=15_000), context)
    assert decision.approved_quantity == 10_000
    assert decision.resized

    cash_engine = RiskEngine(RiskLimits(
        max_single_position_pct=1.0,
        max_gross_exposure_pct=0.95,
        min_cash_pct=0.05,
    ))
    cash_decision = cash_engine.pre_trade_check(
        _request(quantity=10_000),
        _context(cash=100_000, total=1_000_000),
    )
    assert cash_decision.approved_quantity == 5_000
    print("PASS gross exposure/min cash")


def test_stale_quote_and_connection():
    engine = RiskEngine(RiskLimits(max_quote_age_seconds=10))
    stale = engine.pre_trade_check(
        _request(quantity=100),
        _context(quote=_quote(age=20)),
    )
    assert not stale.approved
    assert "STALE_MARKET_DATA" in stale.rule_codes

    disconnected = engine.pre_trade_check(
        _request(quantity=100),
        _context(connected=False),
    )
    assert not disconnected.approved
    assert "BROKER_DISCONNECTED" in disconnected.rule_codes
    assert engine.state is TradingState.STOP_OPEN
    print("PASS stale quote/connection")


def test_account_loss_and_drawdown_actions():
    engine = RiskEngine(RiskLimits(
        max_daily_loss_pct=0.02,
        max_drawdown_pct=0.08,
    ))
    engine.on_account_snapshot(
        AccountState(total_asset=100_000, available_cash=100_000),
        now=NOW,
    )
    engine.on_account_snapshot(
        AccountState(total_asset=97_000, available_cash=97_000),
        now=NOW + dt.timedelta(hours=1),
    )
    assert engine.state is TradingState.STOP_OPEN
    assert engine.daily_return < -0.02
    assert any(item.rule_code == "DAILY_LOSS_LIMIT"
               for item in engine.events)

    drawdown_engine = RiskEngine(RiskLimits(
        max_daily_loss_pct=0.5,
        max_drawdown_pct=0.08,
    ))
    drawdown_engine.on_account_snapshot(
        AccountState(total_asset=100_000, available_cash=100_000),
        now=NOW,
    )
    drawdown_engine.on_account_snapshot(
        AccountState(total_asset=91_000, available_cash=91_000),
        now=NOW + dt.timedelta(days=1),
    )
    assert drawdown_engine.state is TradingState.REDUCE_ONLY
    assert any(item.rule_code == "MAX_DRAWDOWN_LIMIT"
               for item in drawdown_engine.events)
    print("PASS daily loss/drawdown actions")


def test_kill_switch_and_reduce_only():
    engine = RiskEngine()
    context = _context(
        positions=(PositionState(
            symbol="600519",
            total_quantity=100,
            available_quantity=100,
            market_value=1_000,
        ),)
    )
    engine.enable_reduce_only("manual reduce only")
    assert not engine.pre_trade_check(_request(), context).approved
    assert engine.pre_trade_check(_request(OrderSide.SELL, 100), context).approved

    engine.enable_kill_switch("manual emergency stop")
    assert engine.state is TradingState.KILLED
    assert not engine.pre_trade_check(_request(OrderSide.SELL, 100), context).approved
    engine.recover(reason="confirmed flat")
    assert engine.state is TradingState.ACTIVE
    print("PASS kill switch/reduce only")


def test_mock_broker_risk_integration():
    broker = MockBroker(fill_mode="none", initial_cash=1_000_000)
    broker.connect()
    quote = _quote()
    engine = RiskEngine(RiskLimits(
        max_single_position_pct=0.2,
        max_gross_exposure_pct=0.8,
        min_cash_pct=0.05,
    ))
    decision, order_id = submit_with_risk(
        broker,
        engine,
        _request(quantity=30_000),
        {"600519": quote},
        now=NOW,
    )
    assert decision.resized and decision.approved_quantity == 20_000
    assert order_id is not None
    broker.fill_order(order_id, 100, 10.0)
    assert broker.get_order(order_id).filled_quantity == 100
    context = risk_context_from_broker(
        broker, {"600519": quote}, now=NOW
    )
    assert context.account.total_asset > 0
    assert context.position_map()["600519"].available_quantity == 100
    broker.disconnect()
    print("PASS MockBroker risk integration")


def test_kill_switch_cancels_broker_orders():
    broker = MockBroker(fill_mode="none")
    broker.connect()
    order_id = broker.order_buy("600519", 10.0, 100)
    engine = RiskEngine()
    cancelled = execute_kill_switch(broker, engine, "test emergency")
    assert cancelled == 1
    assert engine.state is TradingState.KILLED
    assert broker.get_order(order_id).status.value == "cancelled"
    assert any(
        item.rule_code == "KILL_SWITCH_CANCEL_ALL"
        for item in engine.events
    )
    broker.disconnect()
    print("PASS kill switch broker cancellation")


def run_test():
    for test in (
        test_pretrade_cash_lot_and_position_limits,
        test_t_plus_one_and_price_limits,
        test_gross_exposure_and_min_cash,
        test_stale_quote_and_connection,
        test_account_loss_and_drawdown_actions,
        test_kill_switch_and_reduce_only,
        test_mock_broker_risk_integration,
        test_kill_switch_cancels_broker_orders,
    ):
        test()
    print("===== risk engine tests passed =====")


if __name__ == "__main__":
    run_test()
