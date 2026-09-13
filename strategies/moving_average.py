"""双均线策略（旧入口，保留兼容）

实现已迁移到 strategies/double_ma.py，本文件只保留旧类名，
已有代码（如 from strategies.moving_average import MovingAverageStrategy）不受影响。
"""

from strategies.double_ma import DoubleMAStrategy

MovingAverageStrategy = DoubleMAStrategy

__all__ = ["MovingAverageStrategy"]
