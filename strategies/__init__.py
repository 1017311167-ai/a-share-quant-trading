"""strategies 包 —— 策略库

每个策略文件实现一种交易策略。策略约定：
    输入：行情数据 DataFrame
    输出：买卖信号（entries = 买入信号，exits = 卖出信号）

已有策略：
    double_ma.py       双均线策略（金叉买入 / 死叉卖出）
    rsi_strategy.py    RSI 超买超卖策略
    boll_strategy.py   布林带突破策略
    momentum.py        动量策略（过去 N 日涨幅达标买入 / 跌破均线卖出）
    turtle_strategy.py 海龟策略（唐奇安通道突破）

便捷入口（策略工厂，按名称创建策略）：
    from strategies import create_strategy
    s = create_strategy("双均线", fast=10, slow=60)
"""

from strategies.base import Strategy
from strategies.boll_strategy import BollStrategy
from strategies.double_ma import DoubleMAStrategy
from strategies.factory import create_strategy, get_available_strategies
from strategies.momentum import MomentumStrategy
from strategies.rsi_strategy import RSIStrategy
from strategies.turtle_strategy import TurtleStrategy

__all__ = [
    "Strategy",
    "DoubleMAStrategy",
    "RSIStrategy",
    "BollStrategy",
    "MomentumStrategy",
    "TurtleStrategy",
    "create_strategy",
    "get_available_strategies",
]
