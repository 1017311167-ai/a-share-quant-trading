"""遗传算法参数优化（基于 DEAP）

把参数组合看成"个体"，目标指标是"适应度"：每一代选出表现好的个体，
两两交叉、随机变异，产生下一代，一代代进化出最优参数。
适合参数范围大、组合爆炸（网格跑不动）的场景，用少量回测次数找到近似最优解。

用法:
    from optimization.genetic_algo import genetic_optimize

    out = genetic_optimize(df, "双均线",
                           {"fast": range(2, 60), "slow": range(10, 120, 2)},
                           metric="夏普比率",
                           pop_size=30, n_generations=20, seed=42)
    print(out["best_params"])   # 最优参数组合
    print(out["best_value"])    # 最优目标指标值
    print(out["results"])       # 实际评估过的所有组合

运行方式:
    python optimization/genetic_algo.py
"""

import logging
import math
import os
import random
import sys

# 保证直接运行本文件时能找到 optimization / core / strategies / utils 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from deap import base, creator, tools

from optimization._common import (METRIC_COLUMNS, build_engine_kwargs, direction,
                                  normalize_metric, parse_ranges, run_tasks, safe_float)

logger = logging.getLogger("optimization.genetic")

# DEAP 的 creator 只能全局注册一次；模块被重复导入时
# （直接运行 = __main__，再从包导入一次 = optimization.genetic_algo），
# 用 hasattr 判断跳过，避免重复注册的警告
if not hasattr(creator, "FitnessOptMax"):
    creator.create("FitnessOptMax", base.Fitness, weights=(1.0,))
if not hasattr(creator, "IndividualOpt"):
    creator.create("IndividualOpt", list, fitness=creator.FitnessOptMax)


def _cache_key(params: dict) -> tuple:
    """参数组合转缓存键（浮点统一 4 位小数，避免 1.0 与 1.0000001 重复评估）"""
    return tuple(round(v, 4) if isinstance(v, float) else v for v in params.values())


def _params_str(params: dict) -> str:
    """参数字典转展示字符串"""
    return "、".join(f"{k}={v:g}" if isinstance(v, float) else f"{k}={v}"
                     for k, v in params.items())


def _mutate(individual, bounds, mutpb):
    """变异：每个基因以 mutpb 概率在取值范围内重新随机取值"""
    for i, (lo, hi) in enumerate(bounds.values()):
        if random.random() < mutpb:
            if isinstance(lo, int) and isinstance(hi, int):
                individual[i] = random.randint(int(lo), int(hi))
            else:
                individual[i] = random.uniform(float(lo), float(hi))
    return individual,


def _make_toolbox(param_names, grid, mutpb):
    """按参数类型（整数/浮点）注册 DEAP 工具箱"""
    bounds = {}
    for key in param_names:
        vals = grid[key]
        if not all(isinstance(v, (int, float)) for v in vals):
            raise ValueError(f"遗传算法只支持数值参数，{key} 的候选值含非数字：{vals}")
        lo, hi = min(vals), max(vals)
        bounds[key] = (int(lo), int(hi)) if all(isinstance(v, int) for v in vals) else (float(lo), float(hi))

    toolbox = base.Toolbox()
    attrs = []
    for key in param_names:
        lo, hi = bounds[key]
        attr = f"attr_{key}"
        if isinstance(lo, int):
            toolbox.register(attr, random.randint, lo, hi)
        else:
            toolbox.register(attr, random.uniform, lo, hi)
        attrs.append(getattr(toolbox, attr))
    toolbox.register("individual", tools.initCycle, creator.IndividualOpt, tuple(attrs), n=1)
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)
    toolbox.register("select", tools.selTournament, tournsize=3)
    toolbox.register("mate", tools.cxTwoPoint)
    toolbox.register("mutate", _mutate, bounds=bounds, mutpb=mutpb)
    return toolbox


def _eval_population(pop, param_names, df, strategy_name, metric, engine_kwargs,
                     cache, max_workers, progress_cb=None):
    """评估种群：没评估过的组合先回测（已评估的直接查缓存），再把适应度写回个体

    返回:
        (本代新增的有效结果行列表, 本代无效个体列表 [(参数 dict, 错误信息), ...])
    """
    sign = direction(metric)
    tasks, keys = [], []
    for ind in pop:
        params = dict(zip(param_names, ind))
        key = _cache_key(params)
        if key not in cache:
            cache[key] = None  # 先占位，避免同代重复评估
            keys.append(key)
            tasks.append((params, df, strategy_name, engine_kwargs))

    rows, failed = run_tasks(tasks, max_workers, progress_cb)
    for row in rows:
        cache[_cache_key({k: row[k] for k in param_names})] = row
    for params, err in failed:
        cache[_cache_key(params)] = None
        logger.warning(f"个体无效，跳过：{params}（{err}）")

    for ind in pop:
        entry = cache[_cache_key(dict(zip(param_names, ind)))]
        if entry is None:
            ind.fitness.values = (float("-inf"),)
            continue
        v = entry[metric]
        if v is None or (isinstance(v, float) and math.isnan(v)):
            ind.fitness.values = (float("-inf"),)
        else:
            ind.fitness.values = (sign * float(v),)
    return rows, failed


def genetic_optimize(df, strategy_name, param_ranges, metric: str = "夏普比率", *,
                     init_cash=1_000_000, commission=0.00025, slippage=0.001,
                     code=None, rf=0.0, t_plus_1=True, price_limit=True,
                     stamp_tax=True, transfer_fee=True,
                     commission_min=5.0, participation_rate=0.05,
                     impact_coefficient=0.02, max_slippage=0.05,
                     limit_queue_fill_ratio=0.25, order_ttl_bars=5,
                     execution_model="realistic", trade_unit=100,
                     pop_size=20, n_generations=10, cxpb=0.5, mutpb=0.2,
                     seed=None, max_workers=None, verbose=True,
                     progress_cb=None) -> dict:
    """遗传算法寻优：最大化（或最小化）目标指标

    参数:
        df:             单只股票行情数据
        strategy_name:  策略名（如 "双均线"）
        param_ranges:   参数范围字典（同网格搜索，四种写法都支持）
        metric:         优化目标指标（默认 夏普比率）
        init_cash / commission / slippage / code / rf / t_plus_1 /
        price_limit / stamp_tax / transfer_fee: 回测全局参数，同回测引擎
        pop_size:       种群大小（每代个体数），默认 20
        n_generations:  迭代次数（进化代数），默认 10
        cxpb:           交叉概率（两个个体交换基因片段的概率），默认 0.5
        mutpb:          变异概率（每个基因随机重置的概率），默认 0.2
        seed:           随机种子（设置后结果可复现），默认 None
        max_workers:    并发进程数；None 自动，1 串行
        verbose:        是否每代打印当前最优解
        progress_cb:    进度回调 progress_cb(已完成数, 总数)，供界面进度条使用

    返回:
        {"best_params": 最优参数组合, "best_value": 最优目标指标值,
         "best_metrics": 最优组合的完整绩效指标,
         "results": 所有实际评估过的组合汇总表（最优在前）,
         "failed": 无效组合列表, "metric": 目标指标}
    """
    if not logger.handlers:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%H:%M:%S")
    metric = normalize_metric(metric)
    for name, val in (("pop_size", pop_size), ("n_generations", n_generations)):
        if not isinstance(val, int) or val < 1:
            raise ValueError(f"{name} 必须为正整数：{val!r}")
    for name, val in (("cxpb", cxpb), ("mutpb", mutpb)):
        if not 0 <= val <= 1:
            raise ValueError(f"{name} 必须在 [0, 1] 之间：{val!r}")
    if seed is not None:
        random.seed(seed)

    grid = parse_ranges(param_ranges)
    param_names = list(grid.keys())
    engine_kwargs = build_engine_kwargs(init_cash, commission, slippage, code, rf,
                                        t_plus_1, price_limit, stamp_tax, transfer_fee,
                                        commission_min, participation_rate,
                                        impact_coefficient, max_slippage,
                                        limit_queue_fill_ratio, order_ttl_bars,
                                        execution_model, trade_unit)
    toolbox = _make_toolbox(param_names, grid, mutpb)

    pop = toolbox.population(n=pop_size)
    hof = tools.HallOfFame(1)   # 名人堂：保存历史最优个体
    cache = {}                  # 组合 -> 评估结果（同一组合跨代不重复回测）
    all_rows = []
    all_failed = []             # 所有代里无效的个体

    if verbose:
        logger.info(f"遗传算法：种群 {pop_size}，迭代 {n_generations} 代，目标 {metric}"
                    f"（seed={seed}，交叉 {cxpb}，变异 {mutpb}）")

    for gen in range(n_generations + 1):  # 第 0 代 = 初始种群，同样评估记录
        rows, failed = _eval_population(pop, param_names, df, strategy_name, metric,
                                        engine_kwargs, cache, max_workers, progress_cb)
        all_rows.extend(rows)
        all_failed.extend(failed)
        hof.update(pop)
        best_ind = hof[0]
        best_params = dict(zip(param_names, best_ind))
        fits = [ind.fitness.values[0] for ind in pop]
        if verbose:
            mean = sum(fits) / len(fits) if fits else float("-inf")
            logger.info(f"第 {gen}/{n_generations} 代：最优 {metric}="
                        f"{best_ind.fitness.values[0]:.4f}（{_params_str(best_params)}），"
                        f"本代均值 {mean:.4f}，新增评估 {len(rows)} 组"
                        + (f"，无效 {len(failed)} 组" if failed else ""))

        if gen == n_generations:
            break
        # 进化：锦标赛选择 -> 交叉 -> 变异，得到下一代
        offspring = [toolbox.clone(ind) for ind in toolbox.select(pop, len(pop))]
        for c1, c2 in zip(offspring[::2], offspring[1::2]):
            if random.random() < cxpb:
                toolbox.mate(c1, c2)
                del c1.fitness.values
                del c2.fitness.values
        for ind in offspring:
            toolbox.mutate(ind)
            del ind.fitness.values
        pop = offspring

    if not all_rows:
        raise ValueError("所有个体都无效，遗传算法无法完成寻优")
    if hof[0].fitness.values[0] == float("-inf"):
        raise ValueError("所有个体都无效，遗传算法无法完成寻优")

    results = pd.DataFrame(all_rows)
    sign = direction(metric)
    tie_columns = [column for column in results.columns if column != metric]
    results = (
        results.assign(_score=results[metric] * sign)
        .sort_values(
            ["_score", *tie_columns],
            ascending=[False, *([True] * len(tie_columns))],
            kind="stable",
        )
        .drop(columns="_score")
        .reset_index(drop=True)
    )

    # 注意：results.iloc[0] 取整行会把 int 参数列也转成 float，
    # 所以按列取值，再按参数范围里的原始类型转回 int/float
    best_params = {}
    for k in param_names:
        v = results[k].iloc[0]
        best_params[k] = int(v) if isinstance(grid[k][0], int) else float(v)
    out = {
        "best_params": best_params,
        "best_value": safe_float(results[metric].iloc[0]),
        "best_metrics": {c: safe_float(results[c].iloc[0]) for c in METRIC_COLUMNS},
        "results": results,
        "failed": [f"{params}（{err}）" for params, err in all_failed],
        "metric": metric,
    }
    if verbose:
        logger.info(f"遗传算法完成：最优 {metric} = {out['best_value']:.4f}，"
                    f"参数 {out['best_params']}（共评估 {len(results)} 个组合）")
    return out


def run_test():
    """测试：贵州茅台双均线遗传算法寻优（最大化夏普比率）

    运行方式（在项目根目录下）：
        python optimization/genetic_algo.py
    """
    import datetime

    from optimization import optimize_parameters
    from utils.data_loader import load_daily_data

    print("===== 遗传算法测试开始 =====")
    end = datetime.date.today()
    start = end - datetime.timedelta(days=3 * 365)
    df = load_daily_data("600519", start, end)

    # 1. 基本寻优（fast 29 档 × slow 20 档 = 580 组，网格也能跑；遗传用更少回测次数逼近）
    out = genetic_optimize(df, "双均线",
                           {"fast": range(2, 31), "slow": range(30, 90, 3)},
                           metric="夏普比率", code="600519",
                           pop_size=20, n_generations=8, seed=42, max_workers=4)
    best, results = out["best_params"], out["results"]
    assert set(best) == {"fast", "slow"} and isinstance(best["fast"], int)
    assert 2 <= best["fast"] < best["slow"] <= 90, "最优参数应在范围内且 fast < slow"
    assert len(results) >= 20, "至少应评估过初始种群"
    assert abs(out["best_value"] - results["夏普比率"].max()) < 1e-12, "最优值应与汇总表一致"
    assert set(out["best_metrics"]) == set(METRIC_COLUMNS), "最优指标应完整"
    print(f"  ✓ 遗传算法：20 种群 × 8 代，去重后实际评估 {len(results)} 组，"
          f"最优 fast={best['fast']}、slow={best['slow']}，夏普 {out['best_value']:.4f}")

    # 2. 可复现性：同 seed 串行跑两次，最优参数一致
    ranges = {"fast": range(2, 31), "slow": range(30, 90, 3)}
    kwargs = dict(metric="夏普比率", code="600519", pop_size=12, n_generations=5,
                  seed=7, max_workers=1, verbose=False)
    out_a = genetic_optimize(df, "双均线", ranges, **kwargs)
    out_b = genetic_optimize(df, "双均线", ranges, **kwargs)
    assert out_a["best_params"] == out_b["best_params"], "同 seed 最优参数应一致"
    assert abs(out_a["best_value"] - out_b["best_value"]) < 1e-12
    print(f"  ✓ 可复现性：同 seed 两次结果一致（夏普 {out_a['best_value']:.4f}）")

    # 3. 最小化指标：最大回撤越小越好（遗传自动按方向寻优）
    out_dd = genetic_optimize(df, "双均线", {"fast": range(5, 20), "slow": range(20, 60, 5)},
                              metric="最大回撤", code="600519",
                              pop_size=12, n_generations=4, seed=1, max_workers=2, verbose=False)
    assert out_dd["best_value"] == out_dd["results"]["最大回撤"].min(), "回撤寻优应取最小值"
    print(f"  ✓ 最小化指标：最大回撤寻优 best={out_dd['best_value']:.2%}")

    # 4. 统一入口（method='genetic'）
    out2 = optimize_parameters(df, "双均线", {"fast": range(5, 20), "slow": range(20, 60, 5)},
                               metric="夏普比率", method="genetic",
                               pop_size=10, n_generations=3, seed=3,
                               max_workers=2, verbose=False)
    assert out2["method"] == "genetic" and out2["best_params"]
    print(f"  ✓ 统一入口 optimize_parameters(method='genetic')：最优 {out2['best_params']}")

    print("===== 全部测试通过 =====")


if __name__ == "__main__":
    run_test()
