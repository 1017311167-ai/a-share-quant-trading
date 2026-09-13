"""现实成交模型和成本敏感性离线测试。"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.backtest_engine import BacktestEngine
from core.cost_analysis import (
    cost_sensitivity_analysis,
    summarize_cost_sensitivity,
)


def _flat_frame(periods=8, volume=10_000_000):
    index = pd.date_range("2024-01-01", periods=periods, freq="D")
    frame = pd.DataFrame({
        "open": 10.0,
        "high": 10.0,
        "low": 10.0,
        "close": 10.0,
        "volume": volume,
    }, index=index)
    return frame


def _signals(index, entry_idx, exit_idx):
    entries = pd.Series(False, index=index)
    exits = pd.Series(False, index=index)
    entries.iloc[entry_idx] = True
    exits.iloc[exit_idx] = True
    return entries, exits


def test_no_lookahead_execution():
    frame = _flat_frame()
    entries, exits = _signals(frame.index, 1, 3)
    result = BacktestEngine(
        frame,
        entries,
        exits,
        commission=0,
        commission_min=0,
        slippage=0,
        impact_coefficient=0,
        participation_rate=1.0,
        t_plus_1=False,
        price_limit=False,
        stamp_tax=False,
        transfer_fee=False,
    ).run()
    fills = result.get_fills()
    assert list(fills["方向"]) == ["买入", "卖出"]
    assert fills.iloc[0]["日期"] == frame.index[2]
    assert fills.iloc[1]["日期"] == frame.index[4]
    print("PASS no lookahead execution")


def test_minimum_commission():
    frame = _flat_frame()
    entries, exits = _signals(frame.index, 0, 3)
    result = BacktestEngine(
        frame,
        entries,
        exits,
        init_cash=100_000,
        commission=0.00001,
        commission_min=5,
        slippage=0,
        impact_coefficient=0,
        participation_rate=1.0,
        t_plus_1=False,
        price_limit=False,
        stamp_tax=False,
        transfer_fee=False,
    ).run()
    stats = result.get_execution_stats()
    assert stats["total_commission"] == 10
    assert result.get_fills()["佣金"].tolist() == [5.0, 5.0]
    print("PASS minimum commission")


def test_volume_participation_and_partial_fill():
    frame = _flat_frame(volume=1_000)
    entries, exits = _signals(frame.index, 0, -1)
    result = BacktestEngine(
        frame,
        entries,
        exits,
        init_cash=100_000,
        commission=0.00025,
        slippage=0,
        impact_coefficient=0,
        participation_rate=0.1,
        order_ttl_bars=3,
        t_plus_1=False,
        price_limit=False,
    ).run()
    fills = result.get_fills()
    orders = result.get_orders()
    assert fills["数量"].tolist() == [100, 100, 100]
    assert orders.iloc[0]["status"] == "CANCELLED"
    assert orders.iloc[0]["reason"] == "TTL_EXPIRED"
    assert result.get_execution_stats()["partial_order_count"] == 1
    assert result.get_execution_stats()["total_commission"] == 5.0
    print("PASS volume participation/partial fill")


def test_suspension_skips_execution_bar():
    frame = _flat_frame(periods=5)
    frame["trade_status"] = "normal"
    frame.loc[frame.index[2], "trade_status"] = "停牌"
    entries, exits = _signals(frame.index, 1, -1)
    result = BacktestEngine(
        frame,
        entries,
        exits,
        commission=0,
        commission_min=0,
        slippage=0,
        impact_coefficient=0,
        participation_rate=1.0,
        t_plus_1=False,
        price_limit=False,
        stamp_tax=False,
        transfer_fee=False,
    ).run()
    fills = result.get_fills()
    assert fills.iloc[0]["日期"] == frame.index[3]
    print("PASS suspension handling")


def test_limit_queue_defers_sealed_board():
    frame = _flat_frame(periods=6)
    frame.loc[frame.index[2], ["open", "high", "low", "close"]] = 11.0
    entries, exits = _signals(frame.index, 1, -1)
    result = BacktestEngine(
        frame,
        entries,
        exits,
        commission=0,
        commission_min=0,
        slippage=0,
        impact_coefficient=0,
        participation_rate=1.0,
        code="600519",
        t_plus_1=False,
        price_limit=True,
        stamp_tax=False,
        transfer_fee=False,
    ).run()
    fills = result.get_fills()
    assert fills.iloc[0]["日期"] == frame.index[3]
    print("PASS limit-up queue")


def test_limit_open_allows_queue_ratio_fill():
    frame = _flat_frame(periods=5, volume=10_000)
    frame.loc[frame.index[2], ["open", "high", "close"]] = 11.0
    frame.loc[frame.index[2], "low"] = 10.5
    entries, exits = _signals(frame.index, 1, -1)
    result = BacktestEngine(
        frame,
        entries,
        exits,
        init_cash=100_000,
        commission=0,
        commission_min=0,
        slippage=0,
        impact_coefficient=0,
        participation_rate=1.0,
        limit_queue_fill_ratio=0.25,
        code="600519",
        t_plus_1=False,
        price_limit=True,
        stamp_tax=False,
        transfer_fee=False,
    ).run()
    fills = result.get_fills()
    assert fills.iloc[0]["日期"] == frame.index[2]
    assert fills.iloc[0]["数量"] == 2500
    print("PASS limit open queue ratio")


def test_cost_sensitivity_report():
    frame = _flat_frame(periods=40, volume=1_000_000)
    close = np.linspace(10, 15, len(frame))
    frame["open"] = close
    frame["high"] = close + 0.1
    frame["low"] = close - 0.1
    frame["close"] = close
    entries, exits = _signals(frame.index, 1, 30)
    report = cost_sensitivity_analysis(
        frame,
        entries,
        exits,
        base_kwargs={
            "code": "600519",
            "t_plus_1": False,
            "price_limit": False,
        },
        commission_multipliers=(1.0, 2.0),
        slippage_values=(0.0, 0.002),
        participation_rates=(0.05, 0.2),
        min_commission_values=(0.0, 5.0),
    )
    assert {"基准", "零交易成本"} <= set(report["场景"])
    assert "相对零成本拖累" in report.columns
    assert (report["总手续费"] >= 0).all()
    summary = summarize_cost_sensitivity(report)
    assert "基准成本拖累" in summary
    print("PASS cost sensitivity")


def run_test():
    for test in (
        test_no_lookahead_execution,
        test_minimum_commission,
        test_volume_participation_and_partial_fill,
        test_suspension_skips_execution_bar,
        test_limit_queue_defers_sealed_board,
        test_limit_open_allows_queue_ratio_fill,
        test_cost_sensitivity_report,
    ):
        test()
    print("===== execution model tests passed =====")


if __name__ == "__main__":
    run_test()
