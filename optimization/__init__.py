"""参数优化包 —— 网格搜索 + 遗传算法，统一入口

用法:
    from optimization import optimize_parameters

    # 网格搜索：穷举所有参数组合（组合不多时首选）
    out = optimize_parameters(df, "双均线",
                              {"fast": range(5, 20), "slow": range(20, 60, 5)},
                              metric="夏普比率", method="grid")

    # 遗传算法：参数范围大、组合爆炸时用（DEAP，支持随机种子复现）
    out = optimize_parameters(df, "双均线",
                              {"fast": range(2, 60), "slow": range(10, 120, 2)},
                              metric="夏普比率", method="genetic",
                              pop_size=30, n_generations=20, seed=42)

两个方法返回结构完全一致：
    {"best_params": 最优参数, "best_value": 最优目标指标值,
     "best_metrics": 最优组合完整绩效指标, "results": 所有测试结果汇总表,
     "failed": 无效组合列表, "metric": 目标指标, "method": 寻优方法}
"""

import os
import sys

# 保证直接运行本目录下文件时能找到 optimization / core 等包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from optimization._common import METRIC_COLUMNS, MINIMIZE_METRICS, parse_ranges
from optimization.genetic_algo import genetic_optimize
from optimization.grid_search import grid_search

__all__ = ["optimize_parameters", "grid_search", "genetic_optimize",
           "METRIC_COLUMNS", "MINIMIZE_METRICS", "parse_ranges"]


def optimize_parameters(df, strategy_name, param_ranges, metric: str = "夏普比率",
                        method: str = "grid", **kwargs) -> dict:
    """统一参数优化入口

    参数:
        df:             单只股票行情数据（load_daily_data 返回的 DataFrame）
        strategy_name:  策略名（如 "双均线"、"RSI超买超卖"）
        param_ranges:   参数范围字典，如 {"fast": range(5, 20), "slow": range(20, 60, 5)}
        metric:         优化目标指标（默认 夏普比率；最大回撤/年化波动率/总手续费 越小越好）
        method:         "grid" 网格搜索 / "genetic" 遗传算法
        **kwargs:       透传给具体方法，共用的有：
                            init_cash / commission / slippage / code / rf /
                            t_plus_1 / price_limit / stamp_tax / transfer_fee /
                            max_workers / verbose
                        grid 专属：max_combos
                        genetic 专属：pop_size / n_generations / cxpb / mutpb / seed

    返回:
        {"best_params", "best_value", "best_metrics", "results",
         "failed", "metric", "method"}
    """
    if method == "grid":
        out = grid_search(df, strategy_name, param_ranges, metric=metric, **kwargs)
    elif method in ("genetic", "ga"):
        out = genetic_optimize(df, strategy_name, param_ranges, metric=metric, **kwargs)
    else:
        raise ValueError(f"未知寻优方法：{method!r}，可选：'grid' / 'genetic'")
    out["method"] = method
    return out
