"""RSI 超买超卖策略

原理：RSI 是衡量涨跌力度的摆动指标，取值 0~100。
- RSI 从超卖区向上穿越阈值时买入（超跌反弹）
- RSI 从超买区向下穿越阈值时卖出（涨多回调）
属于均值回归类策略，适合震荡行情。
"""

import pandas as pd

from strategies.base import Strategy
from strategies.metadata import ParameterSpec


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """计算 RSI（Wilder 平滑），返回与 close 同索引的 Series

    参数:
        close:  收盘价序列
        period: 计算周期，默认 14
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - 100 / (1 + rs)
    # 纯上涨段（跌幅均值为 0）：RSI = 100；涨跌全为 0 的段保持 NaN（无信号）
    return rsi.mask((avg_loss == 0) & (avg_gain > 0), 100.0)


class RSIStrategy(Strategy):
    """RSI 超买超卖策略"""

    strategy_key = "rsi"
    name = "RSI超买超卖策略"
    version = "1.0.0"
    description = "RSI 上穿超卖阈值买入，下穿超买阈值卖出"
    required_columns = ("close",)
    tags = ("mean-reversion", "oscillator")
    constraints = ("oversold < overbought",)
    PARAMETER_SPECS = (
        ParameterSpec(
            "period", 14, "RSI 计算周期（天），默认 14",
            kind="int", minimum=2, step=1,
        ),
        ParameterSpec(
            "oversold", 30.0, "超卖阈值（0~100），默认 30",
            kind="float", minimum=0.0, maximum=100.0,
            exclusive_minimum=True, exclusive_maximum=True, step=1.0,
        ),
        ParameterSpec(
            "overbought", 70.0, "超买阈值（0~100），默认 70",
            kind="float", minimum=0.0, maximum=100.0,
            exclusive_minimum=True, exclusive_maximum=True, step=1.0,
        ),
    )

    def __init__(self, period: int = 14, oversold: float = 30, overbought: float = 70):
        if not isinstance(period, int) or period < 2:
            raise ValueError(f"period 必须为不小于 2 的整数：{period!r}")
        if not isinstance(oversold, (int, float)) or not 0 < oversold < 100:
            raise ValueError(f"oversold 必须在 (0, 100) 之间：{oversold!r}")
        if not isinstance(overbought, (int, float)) or not 0 < overbought < 100:
            raise ValueError(f"overbought 必须在 (0, 100) 之间：{overbought!r}")
        if oversold >= overbought:
            raise ValueError(f"oversold({oversold}) 必须小于 overbought({overbought})")
        self.period = period
        self.oversold = float(oversold)
        self.overbought = float(overbought)

    def generate_signals(self, df: pd.DataFrame):
        """计算 RSI 穿越信号

        参数:
            df: 行情数据，至少包含 close 列

        返回:
            (entries, exits)：买入/卖出信号，与 df 等长的布尔 Series
        """
        rsi = compute_rsi(df["close"], self.period)

        entries = (rsi > self.oversold) & (rsi.shift(1) <= self.oversold)      # 上穿超卖线：买入
        exits = (rsi < self.overbought) & (rsi.shift(1) >= self.overbought)    # 下穿超买线：卖出

        return entries.fillna(False), exits.fillna(False)
