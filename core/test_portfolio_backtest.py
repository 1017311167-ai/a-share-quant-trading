"""组合回测离线测试。"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.portfolio_backtest import (
    PortfolioBacktestEngine,
    PortfolioConstraints,
    constrain_target_weights,
    signals_to_target_weights,
)


def _data(periods=90):
    index = pd.date_range("2024-01-01", periods=periods, freq="D")
    data = {}
    for offset, symbol in enumerate(("AAA", "BBB", "CCC")):
        close = pd.Series(
            10 + offset + np.linspace(0, 2 + offset, periods),
            index=index,
        )
        data[symbol] = pd.DataFrame({
            "open": close,
            "high": close + 0.1,
            "low": close - 0.1,
            "close": close,
            "volume": 2_000_000,
        }, index=index)
    return index, data


def _signals(index):
    result = {}
    for offset, symbol in enumerate(("AAA", "BBB", "CCC")):
        entries = pd.Series(False, index=index)
        exits = pd.Series(False, index=index)
        entries.iloc[1 + offset] = True
        result[symbol] = (entries, exits)
    return result


def test_target_weight_constraints():
    index = pd.date_range("2024-01-01", periods=3, freq="D")
    symbols = ["AAA", "BBB", "CCC"]
    raw = pd.DataFrame({
        "AAA": [0.7, 0.7, 0.7],
        "BBB": [0.5, 0.5, 0.5],
        "CCC": [0.1, 0.1, 0.1],
    }, index=index)
    constrained = constrain_target_weights(
        raw,
        index,
        symbols,
        PortfolioConstraints(
            max_weight=0.4,
            min_cash_weight=0.1,
            max_gross_exposure=1.0,
            max_positions=2,
        ),
    )
    assert constrained.max().max() <= 0.4 + 1e-12
    assert constrained.sum(axis=1).max() <= 0.9 + 1e-12
    assert (constrained["CCC"] == 0).all()
    print("PASS target weight constraints")


def test_signal_to_equal_weight_targets():
    index, data = _data()
    signals = _signals(index)
    for symbol in data:
        # 将信号索引替换为行情索引，模拟数据加载器常见返回。
        entries, exits = signals[symbol]
        signals[symbol] = (entries, exits)
    targets = signals_to_target_weights(
        signals,
        index,
        list(data),
        constraints=PortfolioConstraints(
            max_weight=0.4,
            min_cash_weight=0.1,
        ),
    )
    assert targets.sum(axis=1).max() <= 0.9 + 1e-12
    assert targets.max().max() <= 0.4 + 1e-12
    assert (targets.iloc[0] == 0).all()
    assert targets.iloc[-1].sum() > 0
    print("PASS signal equal-weight targets")


def test_portfolio_run_cash_and_risk():
    index, data = _data()
    engine = PortfolioBacktestEngine.from_signals(
        data,
        _signals(index),
        rebalance_frequency="D",
        constraints=PortfolioConstraints(
            max_weight=0.4,
            min_cash_weight=0.1,
            max_gross_exposure=1.0,
        ),
        commission=0.00025,
        slippage=0,
        impact_coefficient=0,
        participation_rate=1.0,
        t_plus_1=False,
        price_limit=False,
    ).run()
    metrics = engine.get_metrics()
    cash_ratio = engine.get_cash() / engine.get_equity()
    assert metrics["期末总资产"] > 0
    assert metrics["最大总仓位"] <= 1.0 + 1e-9
    assert cash_ratio.min() >= 0.1 - 1e-6
    assert "日VaR95" in metrics and "日CVaR95" in metrics
    contribution = engine.get_pnl_contribution()
    total_pnl = contribution["组合盈亏（元）"].sum()
    expected = metrics["期末总资产"] - 1_000_000
    assert abs(total_pnl - expected) < 1e-6
    assert len(engine.get_orders()) > 0
    print("PASS portfolio cash/risk")


def test_rebalance_frequency_reduces_trading():
    index, data = _data()
    signals = _signals(index)
    daily = PortfolioBacktestEngine.from_signals(
        data, signals, rebalance_frequency="D",
        constraints=PortfolioConstraints(max_weight=0.4, min_cash_weight=0.1),
        slippage=0, impact_coefficient=0, participation_rate=1.0,
        t_plus_1=False, price_limit=False,
    ).run()
    monthly = PortfolioBacktestEngine.from_signals(
        data, signals, rebalance_frequency="M",
        constraints=PortfolioConstraints(max_weight=0.4, min_cash_weight=0.1),
        slippage=0, impact_coefficient=0, participation_rate=1.0,
        t_plus_1=False, price_limit=False,
    ).run()
    assert len(monthly.get_orders()) <= len(daily.get_orders())
    assert monthly.get_metrics()["累计换手率"] <= daily.get_metrics()["累计换手率"]
    print("PASS rebalance frequency")


def run_test():
    for test in (
        test_target_weight_constraints,
        test_signal_to_equal_weight_targets,
        test_portfolio_run_cash_and_risk,
        test_rebalance_frequency_reduces_trading,
    ):
        test()
    print("===== portfolio backtest tests passed =====")


if __name__ == "__main__":
    run_test()
