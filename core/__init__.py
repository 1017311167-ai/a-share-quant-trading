"""core 包 —— 回测核心

使用事件驱动成交模型实现回测，并提供风控与成本分析：
    from core import BacktestEngine, RiskAnalyzer, analyze_portfolio
    from core.engine import run_backtest

A 股规则包括 T+1、涨跌停、最低佣金、印花税和过户费。
"""

from core.backtest_engine import BacktestEngine
from core.cost_analysis import (
    cost_sensitivity_analysis,
    cost_sensitivity_from_strategy,
    summarize_cost_sensitivity,
)
from core.execution_model import ExecutionConfig, RealisticExecutionSimulator
from core.portfolio_backtest import (
    PortfolioBacktestEngine,
    PortfolioConstraints,
    run_portfolio_backtest,
    signals_to_target_weights,
)
from core.risk_analysis import RiskAnalyzer, analyze_portfolio, stop_loss_atr, stop_loss_take_profit

__all__ = [
    "BacktestEngine",
    "ExecutionConfig",
    "PortfolioBacktestEngine",
    "PortfolioConstraints",
    "RealisticExecutionSimulator",
    "RiskAnalyzer",
    "analyze_portfolio",
    "cost_sensitivity_analysis",
    "cost_sensitivity_from_strategy",
    "summarize_cost_sensitivity",
    "run_portfolio_backtest",
    "signals_to_target_weights",
    "stop_loss_take_profit",
    "stop_loss_atr",
]
