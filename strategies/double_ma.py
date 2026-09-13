"""双均线策略

原理：短期均线上穿长期均线（金叉）时买入，下穿（死叉）时卖出。
属于趋势跟随类策略，适合单边行情；震荡市中会频繁产生假信号。
"""

import pandas as pd

from strategies.base import Strategy


class DoubleMAStrategy(Strategy):
    """双均线策略"""

    name = "双均线策略"

    PARAMS_HELP = {
        "fast": "短期均线周期（天），默认 5",
        "slow": "长期均线周期（天），默认 20，必须大于 fast",
    }

    def __init__(self, fast: int = 5, slow: int = 20):
        if not isinstance(fast, int) or fast < 1:
            raise ValueError(f"fast 必须为正整数：{fast!r}")
        if not isinstance(slow, int) or slow < 1:
            raise ValueError(f"slow 必须为正整数：{slow!r}")
        if fast >= slow:
            raise ValueError(f"fast({fast}) 必须小于 slow({slow})")
        self.fast = fast
        self.slow = slow

    @classmethod
    def default_params(cls) -> dict:
        return {"fast": 5, "slow": 20}

    def generate_signals(self, df: pd.DataFrame):
        """计算金叉/死叉信号

        参数:
            df: 行情数据，至少包含 close 列

        返回:
            (entries, exits)：买入/卖出信号，与 df 等长的布尔 Series
        """
        close = df["close"]
        fast_ma = close.rolling(self.fast).mean()  # 短期均线
        slow_ma = close.rolling(self.slow).mean()  # 长期均线

        entries = (fast_ma > slow_ma) & (fast_ma.shift(1) <= slow_ma.shift(1))  # 金叉：买入
        exits = (fast_ma < slow_ma) & (fast_ma.shift(1) >= slow_ma.shift(1))    # 死叉：卖出

        return entries.fillna(False), exits.fillna(False)
