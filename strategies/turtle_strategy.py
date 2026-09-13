"""海龟策略（唐奇安通道突破）

原理：收盘价突破过去 N 日最高价时买入，跌破过去 M 日最低价时卖出。
经典海龟交易法则的信号简化版，属于趋势突破类策略，
适合趋势明确的市场；震荡市中假突破较多。
"""

import os
import sys

# 保证直接运行本文件（python strategies/turtle_strategy.py）时能找到 strategies 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from strategies.base import Strategy


class TurtleStrategy(Strategy):
    """海龟策略"""

    name = "海龟策略"

    PARAMS_HELP = {
        "entry_window": "入场通道周期（天）：突破过去 N 日最高价买入，默认 20",
        "exit_window": "出场通道周期（天）：跌破过去 M 日最低价卖出，默认 10",
    }

    def __init__(self, entry_window: int = 20, exit_window: int = 10):
        if not isinstance(entry_window, int) or entry_window < 1:
            raise ValueError(f"entry_window 必须为正整数：{entry_window!r}")
        if not isinstance(exit_window, int) or exit_window < 1:
            raise ValueError(f"exit_window 必须为正整数：{exit_window!r}")
        self.entry_window = entry_window
        self.exit_window = exit_window

    @classmethod
    def default_params(cls) -> dict:
        return {"entry_window": 20, "exit_window": 10}

    def generate_signals(self, df: pd.DataFrame):
        """计算唐奇安通道突破信号

        参数:
            df: 行情数据，至少包含 high/low/close 列

        返回:
            (entries, exits)：买入/卖出信号，与 df 等长的布尔 Series
        """
        high, low, close = df["high"], df["low"], df["close"]
        entry_channel = high.shift(1).rolling(self.entry_window).max()  # 前 N 日最高价（不含今天）
        exit_channel = low.shift(1).rolling(self.exit_window).min()     # 前 M 日最低价（不含今天）

        exits = close < exit_channel                                    # 跌破通道离场
        entries = (close > entry_channel) & ~exits                      # 突破通道且当日未触发离场

        return entries.fillna(False), exits.fillna(False)


def run_test():
    """测试：参数校验 + 合成数据信号正确性（不联网）

    运行方式（在项目根目录下）：
        python strategies/turtle_strategy.py
    """
    import numpy as np

    print("===== 海龟策略测试开始 =====")

    # 1. 参数校验
    bad_cases = [{"entry_window": 0}, {"exit_window": 0}, {"exit_window": -3}]
    for kwargs in bad_cases:
        try:
            TurtleStrategy(**kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{kwargs} 应该抛出 ValueError")
    print(f"  ✓ 非法参数校验通过（{len(bad_cases)} 组用例均正确报错）")

    n = 80

    def make_df(last_close):
        close = pd.Series(np.full(n, 10.0))
        close.iloc[-1] = last_close
        return pd.DataFrame({"open": close, "high": close, "low": close, "close": close})

    # 2. 向上突破：收盘价冲过前 N 日最高价 → 当天买入
    up = make_df(15.0)
    entries, exits = TurtleStrategy(entry_window=10, exit_window=5).generate_signals(up)
    assert entries.iloc[-1], "突破前 N 日最高价当天应买入"
    assert not exits.iloc[-1], "突破当天不应同时卖出"
    print(f"  ✓ 向上突破：最后一天买入信号 = {entries.iloc[-1]}")

    # 3. 向下突破：收盘价跌破前 M 日最低价 → 当天卖出
    down = make_df(5.0)
    e2, x2 = TurtleStrategy(entry_window=10, exit_window=5).generate_signals(down)
    assert x2.iloc[-1], "跌破前 M 日最低价当天应卖出"
    assert not e2.iloc[-1], "跌破当天不应同时买入"
    print(f"  ✓ 向下突破：最后一天卖出信号 = {x2.iloc[-1]}")

    # 4. 横盘：不突破通道 → 无买卖信号
    flat = make_df(10.0)
    e3, x3 = TurtleStrategy(entry_window=10, exit_window=5).generate_signals(flat)
    assert e3.sum() == 0 and x3.sum() == 0, "横盘时不应有信号"
    assert (entries & exits).sum() == 0 and (e2 & x2).sum() == 0, "买卖信号不能同一天出现"
    print("  ✓ 横盘：无买卖信号")

    print("===== 全部测试通过 =====")


if __name__ == "__main__":
    run_test()
