"""core 包 —— 回测核心

用 vectorbt 实现回测引擎与风控分析：
    from core import BacktestEngine, RiskAnalyzer, analyze_portfolio
    from core.engine import run_backtest

A 股特有规则（T+1、涨跌停、卖出印花税）留待后续版本实现。
"""

from core.backtest_engine import BacktestEngine
from core.risk_analysis import RiskAnalyzer, analyze_portfolio, stop_loss_atr, stop_loss_take_profit

__all__ = [
    "BacktestEngine",
    "RiskAnalyzer",
    "analyze_portfolio",
    "stop_loss_take_profit",
    "stop_loss_atr",
]
