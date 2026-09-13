"""网格搜索参数优化

把每个参数的所有候选值做笛卡尔积，逐一回测，按目标指标排序。
适合参数组合不多（几百组以内）时的穷举搜索：不遗漏任何组合，结果稳定可靠。

用法:
    from optimization.grid_search import grid_search

    out = grid_search(df, "双均线",
                      {"fast": range(5, 20), "slow": range(20, 60, 5)},
                      metric="夏普比率")
    print(out["best_params"])   # 最优参数组合
    print(out["best_value"])    # 最优目标指标值
    print(out["results"])       # 所有测试结果汇总表

运行方式:
    python optimization/grid_search.py
"""

import itertools
import logging
import os
import sys

# 保证直接运行本文件时能找到 optimization / core / strategies / utils 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from optimization._common import (METRIC_COLUMNS, build_engine_kwargs, direction,
                                  normalize_metric, parse_ranges, run_tasks, safe_float)

logger = logging.getLogger("optimization.grid")

DEFAULT_MAX_COMBOS = 200  # 默认最大组合数，防止参数爆炸


def grid_search(df, strategy_name, param_ranges, metric: str = "夏普比率", *,
                init_cash=1_000_000, commission=0.00025, slippage=0.001,
                code=None, rf=0.0, t_plus_1=True, price_limit=True,
                stamp_tax=True, transfer_fee=True,
                max_combos=DEFAULT_MAX_COMBOS, max_workers=None, verbose=True,
                progress_cb=None) -> dict:
    """网格搜索：遍历所有参数组合逐个回测，按目标指标排序

    参数:
        df:             单只股票行情数据（load_daily_data 返回的 DataFrame）
        strategy_name:  策略名（如 "双均线"、"RSI超买超卖"、"布林带突破"）
        param_ranges:   参数范围字典，如
                        {"fast": range(5, 20), "slow": range(20, 60, 5)}
                        也支持 [候选值列表]、(最小值,最大值,步长)、
                        {"min":..,"max":..,"step":..} 四种写法
        metric:         优化目标指标（默认 夏普比率）；
                        最大回撤/年化波动率/总手续费 越小越好，其余越大越好
        init_cash:      初始资金（元）
        commission:     佣金费率（小数）
        slippage:       滑点（小数）
        code:           股票代码（用于涨跌停幅度判断，可选）
        rf:             无风险利率（年化小数）
        t_plus_1 / price_limit / stamp_tax / transfer_fee: A股规则开关，同回测引擎
        max_combos:     最大组合数上限（默认 200），防止参数爆炸
        max_workers:    并发进程数；None 自动（min(组合数, CPU 核数)），1 串行
        verbose:        是否打印进度日志
        progress_cb:    进度回调 progress_cb(已完成数, 总数)，供界面进度条使用

    返回:
        {"best_params": 最优参数组合, "best_value": 最优目标指标值,
         "best_metrics": 最优组合的完整绩效指标, "results": 所有结果汇总表（最优在前）,
         "failed": 无效组合列表, "metric": 目标指标}
    """
    if not logger.handlers:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%H:%M:%S")
    metric = normalize_metric(metric)
    grid = parse_ranges(param_ranges)

    combos = [dict(zip(grid.keys(), vals)) for vals in itertools.product(*grid.values())]
    if len(combos) > max_combos:
        raise ValueError(f"参数组合共 {len(combos)} 组，超过上限 {max_combos}，请缩小范围")

    engine_kwargs = build_engine_kwargs(init_cash, commission, slippage, code, rf,
                                        t_plus_1, price_limit, stamp_tax, transfer_fee)
    tasks = [(params, df, strategy_name, engine_kwargs) for params in combos]

    if verbose:
        logger.info(f"网格搜索：{len(combos)} 组参数，目标 {metric}，开始回测……")
    rows, failed = run_tasks(tasks, max_workers, progress_cb)
    for params, err in failed:
        logger.warning(f"组合无效，已跳过：{params}（{err}）")
    if not rows:
        raise ValueError(f"所有 {len(combos)} 组参数都无效，无法完成网格搜索")

    results = pd.DataFrame(rows)
    sign = direction(metric)
    results = (results.assign(_score=results[metric] * sign)
               .sort_values("_score", ascending=False, kind="stable")
               .drop(columns="_score").reset_index(drop=True))

    # 注意：results.iloc[0] 取整行会把 int 参数列也转成 float，
    # 所以按列取值，再按参数范围里的原始类型转回 int/float
    best_params = {}
    for k in grid:
        v = results[k].iloc[0]
        best_params[k] = int(v) if isinstance(grid[k][0], int) else float(v)
    out = {
        "best_params": best_params,
        "best_value": safe_float(results[metric].iloc[0]),
        "best_metrics": {c: safe_float(results[c].iloc[0]) for c in METRIC_COLUMNS},
        "results": results,
        "failed": [f"{params}（{err}）" for params, err in failed],
        "metric": metric,
    }
    if verbose:
        logger.info(f"网格搜索完成：最优 {metric} = {out['best_value']:.4f}，"
                    f"参数 {out['best_params']}")
    return out


def run_test():
    """测试：贵州茅台双均线网格寻优（最大化夏普比率）

    运行方式（在项目根目录下）：
        python optimization/grid_search.py
    """
    import datetime

    from optimization import optimize_parameters
    from utils.data_loader import load_daily_data

    print("===== 网格搜索测试开始 =====")
    end = datetime.date.today()
    start = end - datetime.timedelta(days=3 * 365)
    df = load_daily_data("600519", start, end)

    # 1. 网格寻优：fast 15 档 × slow 8 档 = 120 组（全部合法）
    out = grid_search(df, "双均线", {"fast": range(5, 20), "slow": range(20, 60, 5)},
                      metric="夏普比率", code="600519", max_workers=4)
    results, best = out["results"], out["best_params"]
    assert len(results) == 120, f"应有 120 组，实际 {len(results)}"
    assert out["failed"] == [], f"全部组合都应有效，实际失败 {out['failed']}"
    assert set(best) == {"fast", "slow"} and isinstance(best["fast"], int)
    assert best["fast"] < best["slow"], "双均线要求 fast < slow"
    assert abs(out["best_value"] - results["夏普比率"].iloc[0]) < 1e-12, "最优值应排第一"
    assert set(out["best_metrics"]) == set(METRIC_COLUMNS), "最优指标应完整"
    assert (results["夏普比率"].fillna(float("-inf")).diff().dropna() <= 1e-9).all(), \
        "应按夏普降序"
    print(f"  ✓ 网格搜索：120 组全有效，最优 fast={best['fast']}、slow={best['slow']}，"
          f"夏普 {out['best_value']:.4f}")
    print(results.head(5).to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    # 2. 范围写法兼容：range / 列表 / 元组 / 字典，非法范围报错
    assert parse_ranges({"a": range(3, 7)}) == {"a": [3, 4, 5, 6]}
    assert parse_ranges({"a": [1, 5, 9]}) == {"a": [1, 5, 9]}          # 列表 = 候选值
    assert parse_ranges({"a": (1, 9, 4)}) == {"a": [1, 5, 9]}          # 元组 = 最小/最大/步长
    assert parse_ranges({"k": (1.0, 2.0, 0.5)}) == {"k": [1.0, 1.5, 2.0]}
    assert parse_ranges({"k": {"min": 1.0, "max": 2.0, "step": 0.5}}) == {"k": [1.0, 1.5, 2.0]}
    for bad in [{"a": (10, 1, 1)}, {"a": []}, {}, {"a": {"min": 1, "max": 9}}]:
        try:
            parse_ranges(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} 应该报错")
    print("  ✓ 范围解析：range/列表/元组/字典 四种写法正确，非法范围报错")

    # 3. 最大组合数上限（58 × 110 = 6380 组，应直接报错而不是跑）
    try:
        grid_search(df, "双均线", {"fast": range(2, 60), "slow": range(10, 120)},
                    metric="夏普比率")
    except ValueError as e:
        print(f"  ✓ 组合数上限保护：{e}")
    else:
        raise AssertionError("超过 max_combos 应报错")

    # 4. 统一入口（method='grid'）
    out2 = optimize_parameters(df, "双均线", {"fast": [5, 10], "slow": [20, 30]},
                               metric="夏普比率", method="grid", max_workers=2, verbose=False)
    assert out2["method"] == "grid" and len(out2["results"]) == 4
    assert out2["best_params"] and out2["best_metrics"]
    print(f"  ✓ 统一入口 optimize_parameters(method='grid')：{len(out2['results'])} 组，"
          f"最优 {out2['best_params']}")

    print("===== 全部测试通过 =====")


if __name__ == "__main__":
    run_test()
