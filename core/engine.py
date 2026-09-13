"""
回测引擎

完整的类封装在 core/backtest_engine.py（BacktestEngine）。
这里提供简单的函数入口，方便快速使用：
    from core.engine import run_backtest
"""

from core.backtest_engine import BacktestEngine


def run_backtest(df, strategy, **kwargs) -> dict:
    """用策略跑一次回测，返回指标字典

    参数:
        df:       行情数据（含 open/high/low/close/volume 和 date 列）
        strategy: 策略对象，需实现 generate_signals(df) 方法
        **kwargs: 传给 BacktestEngine 的参数，如 init_cash、commission、slippage

    返回:
        指标字典，内容见 BacktestEngine.get_metrics()
    """
    entries, exits = strategy.generate_signals(df)
    bt = BacktestEngine(df, entries, exits, **kwargs)
    bt.run()
    return bt.get_metrics()
