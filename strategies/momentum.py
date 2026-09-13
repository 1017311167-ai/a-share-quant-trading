"""动量策略

原理：过去 N 个交易日涨幅超过阈值时买入（动量确认），
跌破 M 日均线时卖出（趋势走坏离场）。
属于趋势跟随类策略，适合强势上涨行情。
"""

import os
import sys

# 保证直接运行本文件（python strategies/momentum.py）时能找到 strategies 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from strategies.base import Strategy


class MomentumStrategy(Strategy):
    """动量策略"""

    name = "动量策略"

    PARAMS_HELP = {
        "momentum_period": "动量回看天数（过去 N 日涨幅），默认 20",
        "threshold": "入场动量阈值（小数，如 0.05 = 涨 5%），默认 0.05",
        "exit_ma": "出场均线周期（天），跌破卖出，默认 60",
    }

    def __init__(self, momentum_period: int = 20, threshold: float = 0.05, exit_ma: int = 60):
        if not isinstance(momentum_period, int) or momentum_period < 1:
            raise ValueError(f"momentum_period 必须为正整数：{momentum_period!r}")
        if not isinstance(threshold, (int, float)) or threshold <= 0:
            raise ValueError(f"threshold 必须大于 0：{threshold!r}")
        if not isinstance(exit_ma, int) or exit_ma < 1:
            raise ValueError(f"exit_ma 必须为正整数：{exit_ma!r}")
        self.momentum_period = momentum_period
        self.threshold = float(threshold)
        self.exit_ma = exit_ma

    @classmethod
    def default_params(cls) -> dict:
        return {"momentum_period": 20, "threshold": 0.05, "exit_ma": 60}

    def generate_signals(self, df: pd.DataFrame):
        """计算动量买卖信号

        参数:
            df: 行情数据，至少包含 close 列

        返回:
            (entries, exits)：买入/卖出信号，与 df 等长的布尔 Series
        """
        close = df["close"]
        roc = close / close.shift(self.momentum_period) - 1   # 过去 N 日涨幅
        exit_ma = close.rolling(self.exit_ma).mean()

        exits = close < exit_ma                               # 跌破均线离场
        entries = (roc >= self.threshold) & ~exits            # 动量达标且当日未触发离场

        return entries.fillna(False), exits.fillna(False)


def run_test():
    """测试：参数校验 + 合成数据信号正确性（不联网）

    运行方式（在项目根目录下）：
        python strategies/momentum.py
    """
    import numpy as np

    print("===== 动量策略测试开始 =====")

    # 1. 参数校验
    bad_cases = [{"momentum_period": 0}, {"threshold": 0}, {"threshold": -0.1}, {"exit_ma": 0}]
    for kwargs in bad_cases:
        try:
            MomentumStrategy(**kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{kwargs} 应该抛出 ValueError")
    print(f"  ✓ 非法参数校验通过（{len(bad_cases)} 组用例均正确报错）")

    # 2. 连续上涨：动量达标 → 产生买入信号
    n = 120
    up = pd.DataFrame({"close": pd.Series(np.arange(1, n + 1, dtype=float))})
    entries, exits = MomentumStrategy(momentum_period=5, threshold=0.01,
                                      exit_ma=30).generate_signals(up)
    assert entries.sum() > 0, "连续上涨应产生买入信号"
    assert (entries & exits).sum() == 0, "买卖信号不能同一天出现"
    print(f"  ✓ 连续上涨：买入 {entries.sum()} 次")

    # 3. 连续下跌：跌破均线 → 产生卖出信号
    down = pd.DataFrame({"close": pd.Series(np.arange(n, 0, -1, dtype=float))})
    e2, x2 = MomentumStrategy(momentum_period=5, threshold=0.01,
                              exit_ma=30).generate_signals(down)
    assert x2.sum() > 0, "连续下跌应产生卖出信号"
    assert (e2 & x2).sum() == 0, "买卖信号不能同一天出现"
    print(f"  ✓ 连续下跌：卖出 {x2.sum()} 次")

    # 4. 横盘（涨幅不达标）：不应有买入信号
    flat = pd.DataFrame({"close": pd.Series(np.full(n, 10.0))})
    e3, x3 = MomentumStrategy(momentum_period=5, threshold=0.01,
                              exit_ma=30).generate_signals(flat)
    assert e3.sum() == 0, "横盘时动量不达标，不应买入"
    assert x3.sum() == 0, "横盘时未跌破均线，不应卖出"
    print("  ✓ 横盘：无买卖信号")

    print("===== 全部测试通过 =====")


if __name__ == "__main__":
    run_test()
