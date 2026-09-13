"""策略工厂

通过策略名快速创建策略实例，无需关心具体类名：

    from strategies.factory import create_strategy

    s = create_strategy("双均线", fast=10, slow=60)
    entries, exits = s.generate_signals(df)
"""

import datetime
import os
import sys

# 保证直接运行本文件（python strategies/factory.py）时能找到 strategies / utils 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from strategies.base import Strategy
from strategies.boll_strategy import BollStrategy, compute_bollinger
from strategies.double_ma import DoubleMAStrategy
from strategies.momentum import MomentumStrategy
from strategies.rsi_strategy import RSIStrategy, compute_rsi
from strategies.turtle_strategy import TurtleStrategy

# 可用策略列表（顺序即界面展示顺序）
_ALL_STRATEGIES = [DoubleMAStrategy, RSIStrategy, BollStrategy,
                   MomentumStrategy, TurtleStrategy]

# 策略别名表：策略名（中英文均可） -> 策略类
_ALIASES = {
    # 双均线
    "double_ma": DoubleMAStrategy,
    "双均线": DoubleMAStrategy,
    "双均线策略": DoubleMAStrategy,
    "moving_average": DoubleMAStrategy,
    # RSI
    "rsi": RSIStrategy,
    "rsi策略": RSIStrategy,
    "rsi超买超卖": RSIStrategy,
    "超买超卖": RSIStrategy,
    # 布林带
    "boll": BollStrategy,
    "布林带": BollStrategy,
    "布林带突破": BollStrategy,
    "布林带突破策略": BollStrategy,
    # 动量
    "momentum": MomentumStrategy,
    "动量": MomentumStrategy,
    "动量策略": MomentumStrategy,
    "roc": MomentumStrategy,
    # 海龟
    "turtle": TurtleStrategy,
    "海龟": TurtleStrategy,
    "海龟策略": TurtleStrategy,
    "海龟交易": TurtleStrategy,
    "唐奇安": TurtleStrategy,
    "donchian": TurtleStrategy,
    # 类名（get_available_strategies 返回的 key 可直接回传给 create_strategy）
    "doublemastrategy": DoubleMAStrategy,
    "rsistrategy": RSIStrategy,
    "bollstrategy": BollStrategy,
    "momentumstrategy": MomentumStrategy,
    "turtlestrategy": TurtleStrategy,
}


def create_strategy(name: str, **params) -> Strategy:
    """按策略名创建策略实例

    参数:
        name:   策略名，中英文均可（如 "双均线"、"rsi"、"布林带突破"）
        params: 策略参数，原样传给策略的 __init__（如 fast=10, slow=60）

    返回:
        策略实例（Strategy 子类），可直接调用 generate_signals(df)
    """
    key = str(name).strip().lower()
    cls = _ALIASES.get(key)
    if cls is None:
        available = "、".join(f"{s.name}（{s.__name__}）" for s in _ALL_STRATEGIES)
        raise ValueError(f"未知策略：{name!r}。可用策略：{available}")
    return cls(**params)


def get_available_strategies() -> list:
    """返回可用策略清单，供界面下拉框等使用

    每项包含：
        key:            类名（如 "DoubleMAStrategy"）
        name:           显示名称（如 "双均线策略"）
        default_params: 默认参数字典
        params_help:    参数说明字典
    """
    return [
        {
            "key": s.__name__,
            "name": s.name,
            "default_params": s.default_params(),
            "params_help": s.PARAMS_HELP,
        }
        for s in _ALL_STRATEGIES
    ]


def run_test():
    """测试：验证 3 个策略的信号输出与策略工厂

    运行方式（在项目根目录下）：
        python strategies/factory.py
    """
    from utils.data_loader import load_daily_data

    print("===== 策略测试开始 =====")

    # 1. 用真实数据验证三个默认策略
    end = datetime.date.today()
    start = end - datetime.timedelta(days=3 * 365)
    print("下载 600519 近 3 年数据（走本地缓存）……")
    df = load_daily_data("600519", start, end)

    for cls in _ALL_STRATEGIES:
        strategy = cls()
        entries, exits = strategy.generate_signals(df)
        assert isinstance(entries, pd.Series) and isinstance(exits, pd.Series), "信号必须是 Series"
        assert entries.dtype == bool and exits.dtype == bool, "信号必须是布尔型"
        assert len(entries) == len(df), "信号长度必须与行情一致"
        assert entries.index.equals(df.index), "信号索引必须与行情一致"
        assert (entries & exits).sum() == 0, "同一天不允许同时出现买卖信号"
        n_entry, n_exit = entries.sum(), exits.sum()
        assert n_entry > 0, f"{strategy.name} 近 3 年应有买入信号"
        print(f"  ✓ {strategy.name}：买入 {n_entry} 次 / 卖出 {n_exit} 次")

    # 2. 自定义参数
    e10, _ = DoubleMAStrategy(fast=10, slow=60).generate_signals(df)
    print(f"  ✓ 自定义参数 fast=10/slow=60：买入 {e10.sum()} 次")

    # 3. 非法参数校验（应全部抛出 ValueError）
    bad_cases = [
        (DoubleMAStrategy, {"fast": 0}),
        (DoubleMAStrategy, {"fast": 20, "slow": 5}),
        (RSIStrategy, {"period": 1}),
        (RSIStrategy, {"oversold": 80, "overbought": 20}),
        (BollStrategy, {"k": 0}),
        (BollStrategy, {"exit_mode": "x"}),
        (MomentumStrategy, {"momentum_period": 0}),
        (MomentumStrategy, {"threshold": 0}),
        (MomentumStrategy, {"exit_ma": 0}),
        (TurtleStrategy, {"entry_window": 0}),
        (TurtleStrategy, {"exit_window": 0}),
    ]
    for cls, kwargs in bad_cases:
        try:
            cls(**kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{cls.__name__}{kwargs} 应该抛出 ValueError")
    print(f"  ✓ 非法参数校验通过（{len(bad_cases)} 组用例均正确报错）")

    # 4. RSI 计算正确性：连续上涨 = 100，连续下跌 = 0
    up = pd.DataFrame({"close": pd.Series(np.arange(1, 61, dtype=float))})
    down = pd.DataFrame({"close": pd.Series(np.arange(60, 0, -1, dtype=float))})
    assert compute_rsi(up["close"]).iloc[-1] == 100.0, "连续上涨 RSI 应为 100"
    assert compute_rsi(down["close"]).iloc[-1] < 1e-6, "连续下跌 RSI 应接近 0"
    print("  ✓ RSI 计算正确：连续上涨=100，连续下跌=0")

    # 5. 布林带计算正确性：横盘时三轨重合
    #    （前 period-1 行滚动窗口未满是 NaN，比较时按 NaN==NaN 处理）
    flat = pd.Series(np.full(30, 10.0))
    mid, upper, lower = compute_bollinger(flat)
    assert np.allclose(upper, mid, equal_nan=True), "横盘时上轨应与中轨重合"
    assert np.allclose(lower, mid, equal_nan=True), "横盘时下轨应与中轨重合"
    print("  ✓ 布林带计算正确：横盘时三轨重合")

    # 6. 策略工厂
    assert isinstance(create_strategy("双均线"), DoubleMAStrategy)
    s_rsi = create_strategy("rsi", period=7)
    assert isinstance(s_rsi, RSIStrategy) and s_rsi.period == 7
    assert isinstance(create_strategy("boll"), BollStrategy)
    s_boll = create_strategy("布林带突破", exit_mode="lower")
    assert s_boll.exit_mode == "lower"
    assert isinstance(create_strategy("动量"), MomentumStrategy)
    assert isinstance(create_strategy("momentum", threshold=0.1), MomentumStrategy)
    assert isinstance(create_strategy("海龟"), TurtleStrategy)
    assert isinstance(create_strategy("turtle", entry_window=30), TurtleStrategy)
    info = get_available_strategies()
    assert len(info) == 5
    assert all({"key", "name", "default_params", "params_help"} <= set(item) for item in info)
    # key（类名）也必须能直接回传给工厂（桌面界面按 key 传参）
    expected = [DoubleMAStrategy, RSIStrategy, BollStrategy, MomentumStrategy, TurtleStrategy]
    for item, cls in zip(info, expected):
        assert isinstance(create_strategy(item["key"]), cls), f"key {item['key']} 应创建 {cls.__name__}"
    try:
        create_strategy("不存在的策略")
    except ValueError as e:
        print(f"  ✓ 未知策略名报错：{e}")
    else:
        raise AssertionError("未知策略名应该抛出 ValueError")
    print("  ✓ 策略工厂通过（中英文名/参数透传/错误提示）")

    # 7. 与回测引擎端到端联通
    from core.backtest_engine import BacktestEngine

    for cls in _ALL_STRATEGIES:
        strategy = cls()
        entries, exits = strategy.generate_signals(df)
        metrics = BacktestEngine(df, entries, exits).run().get_metrics()
        assert metrics["总交易次数"] > 0
        print(f"  ✓ {strategy.name} 回测联通：累计收益 {metrics['累计收益率']:.2%}，"
              f"交易 {metrics['总交易次数']} 次")

    print("===== 全部测试通过 =====")


if __name__ == "__main__":
    run_test()
