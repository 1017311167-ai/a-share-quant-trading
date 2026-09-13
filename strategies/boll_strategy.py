"""布林带突破策略

原理：布林带由中轨（均线）和上下轨（均值 ± k 倍标准差）构成，
约 95% 的价格落在带内。
- 价格突破上轨说明动能强劲：买入
- 价格跌破中轨（或下轨）说明动能衰竭：卖出
属于趋势突破类策略。
"""

import pandas as pd

from strategies.base import Strategy


def compute_bollinger(close: pd.Series, period: int = 20, k: float = 2.0):
    """计算布林带，返回 (mid, upper, lower) 三个与 close 同索引的 Series

    参数:
        close:  收盘价序列
        period: 布林带周期，默认 20
        k:      上下轨标准差倍数，默认 2.0
    """
    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)  # 总体标准差
    upper = mid + k * std
    lower = mid - k * std
    return mid, upper, lower


class BollStrategy(Strategy):
    """布林带突破策略"""

    name = "布林带突破策略"

    PARAMS_HELP = {
        "period": "布林带周期（天），默认 20",
        "k": "上下轨标准差倍数，默认 2.0",
        "exit_mode": '卖出规则："mid" 跌破中轨卖出（默认）；"lower" 跌破下轨卖出',
    }

    def __init__(self, period: int = 20, k: float = 2.0, exit_mode: str = "mid"):
        if not isinstance(period, int) or period < 2:
            raise ValueError(f"period 必须为不小于 2 的整数：{period!r}")
        if not isinstance(k, (int, float)) or k <= 0:
            raise ValueError(f"k 必须为正数：{k!r}")
        if exit_mode not in ("mid", "lower"):
            raise ValueError(f"exit_mode 只支持 'mid' 或 'lower'：{exit_mode!r}")
        self.period = period
        self.k = float(k)
        self.exit_mode = exit_mode

    @classmethod
    def default_params(cls) -> dict:
        return {"period": 20, "k": 2.0, "exit_mode": "mid"}

    def generate_signals(self, df: pd.DataFrame):
        """计算布林带突破信号

        参数:
            df: 行情数据，至少包含 close 列

        返回:
            (entries, exits)：买入/卖出信号，与 df 等长的布尔 Series
        """
        close = df["close"]
        mid, upper, lower = compute_bollinger(close, self.period, self.k)

        entries = (close > upper) & (close.shift(1) <= upper.shift(1))  # 突破上轨：买入

        if self.exit_mode == "mid":
            exits = (close < mid) & (close.shift(1) >= mid.shift(1))    # 跌破中轨：卖出
        else:
            exits = (close < lower) & (close.shift(1) >= lower.shift(1))  # 跌破下轨：卖出

        return entries.fillna(False), exits.fillna(False)
