"""参数寻优模块

对策略参数做网格搜索（遍历所有参数组合逐个回测），按指定指标排序，
找出历史表现最好的参数组合，并生成二维参数热力图。

用法:
    from core.optimizer import optimize, build_heatmap

    out = optimize(df, DoubleMAStrategy,
                   {"fast": [3, 5, 10], "slow": [10, 20, 30]},
                   metric="夏普比率")
    print(out["results"].head(10))     # 结果表（最优在前）
    print(out["best"])                 # 最优参数行
"""

import itertools
import os
import sys

# 保证直接运行本文件（python core/optimizer.py）时能找到 core / strategies / utils 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import plotly.graph_objects as go

from core.backtest_engine import BacktestEngine

# 可选寻优指标
METRICS = ["累计收益率", "年化收益率", "夏普比率", "卡玛比率", "最大回撤"]

# 各指标的展示格式（表格/热力图用）
METRIC_FMT = {
    "累计收益率": ".1%",
    "年化收益率": ".1%",
    "最大回撤": ".1%",
    "夏普比率": ".2f",
    "卡玛比率": ".2f",
}

_MAX_COMBOS = 200  # 参数组合数上限（防止误操作跑太久）


def ranges_to_grid(ranges: dict) -> dict:
    """把「最小值/最大值/步长」描述转成候选值列表

    参数:
        ranges: {"fast": {"min": 5, "max": 15, "step": 5}, ...}

    返回:
        {"fast": [5, 10, 15], ...}
    """
    grid = {}
    for key, r in ranges.items():
        lo, hi, step = r["min"], r["max"], r["step"]
        if step <= 0 or lo > hi:
            raise ValueError(f"参数 {key} 的寻优范围不合法：{r}")
        vals = []
        v = lo
        while v <= hi + 1e-9:
            vals.append(round(v, 4))
            v += step
        grid[key] = vals
    return grid


def _search(df, strategy_cls, param_grid, metric, engine_kwargs):
    """执行网格搜索，返回 (结果行列表, 跳过的无效组合数)"""
    combos = [dict(zip(param_grid.keys(), vals))
              for vals in itertools.product(*param_grid.values())]
    if len(combos) > _MAX_COMBOS:
        raise ValueError(f"参数组合共 {len(combos)} 组，超过上限 {_MAX_COMBOS}，请缩小范围")

    engine_kwargs = dict(engine_kwargs or {})
    rows, skipped = [], 0
    for params in combos:
        try:
            strategy = strategy_cls(**params)
        except (ValueError, TypeError):
            skipped += 1  # 参数组合非法（如 fast >= slow）
            continue
        entries, exits = strategy.generate_signals(df)
        if entries.sum() == 0:
            skipped += 1  # 无买入信号的组合没有意义
            continue
        bt = BacktestEngine(df, entries, exits, **engine_kwargs).run()
        m = bt.get_metrics()
        calmar = m["年化收益率"] / m["最大回撤"] if m["最大回撤"] > 1e-12 else 0.0
        rows.append({
            **params,
            "累计收益率": m["累计收益率"],
            "年化收益率": m["年化收益率"],
            "最大回撤": m["最大回撤"],
            "夏普比率": m["夏普比率"],
            "卡玛比率": calmar,
            "胜率": m["胜率"],
            "总交易次数": m["总交易次数"],
        })

    if not rows:
        raise ValueError(f"所有 {len(combos)} 组参数都无效（参数非法或无买入信号）")
    return rows, skipped


def grid_search(df, strategy_cls, param_grid, metric: str = "夏普比率",
                engine_kwargs=None) -> pd.DataFrame:
    """遍历参数组合逐个回测，按指标排序

    参数:
        df:           行情数据
        strategy_cls: 策略类（如 DoubleMAStrategy）
        param_grid:   参数候选值字典，如 {"fast": [3, 5, 10], "slow": [10, 20, 30]}
        metric:       排序指标，见 METRICS；最大回撤按从小到大排，其余按从大到小
        engine_kwargs: 传给 BacktestEngine 的参数（如 init_cash、t_plus_1、code）

    返回:
        DataFrame：参数列 + 各绩效指标列，按 metric 排序（最优在前）
    """
    if metric not in METRICS:
        raise ValueError(f"未知寻优指标：{metric!r}，可选：{METRICS}")
    if not param_grid:
        raise ValueError("param_grid 不能为空")

    rows, _ = _search(df, strategy_cls, param_grid, metric, engine_kwargs)
    result = pd.DataFrame(rows)
    ascending = metric == "最大回撤"  # 回撤越小越好
    tie_columns = [column for column in result.columns if column != metric]
    return result.sort_values(
        [metric, *tie_columns],
        ascending=[ascending, *([True] * len(tie_columns))],
        kind="stable",
    ).reset_index(drop=True)


def optimize(df, strategy_cls, param_grid, metric: str = "夏普比率",
             engine_kwargs=None, *, record_experiment: bool = True,
             experiment_name: str | None = None,
             experiment_store=None) -> dict:
    """寻优入口：返回结果表、最优参数行和跳过数

    返回:
        {"results": DataFrame（最优在前）, "best": 最优参数行（Series）,
         "skipped": 跳过的无效组合数, "metric": 排序指标}
    """
    if metric not in METRICS:
        raise ValueError(f"未知寻优指标：{metric!r}，可选：{METRICS}")
    if not param_grid:
        raise ValueError("param_grid 不能为空")

    rows, skipped = _search(df, strategy_cls, param_grid, metric, engine_kwargs)
    result = pd.DataFrame(rows)
    ascending = metric == "最大回撤"
    tie_columns = [column for column in result.columns if column != metric]
    result = result.sort_values(
        [metric, *tie_columns],
        ascending=[ascending, *([True] * len(tie_columns))],
        kind="stable",
    ).reset_index(drop=True)
    out = {
        "results": result,
        "best": result.iloc[0],
        "skipped": skipped,
        "metric": metric,
    }
    if record_experiment:
        from research.experiments import (
            CostAssumptions,
            record_optimization_experiment,
        )

        engine_kwargs = dict(engine_kwargs or {})
        best_row = result.iloc[0]
        best_params = {
            key: int(best_row[key]) if isinstance(param_grid[key][0], int)
            else float(best_row[key])
            for key in param_grid
        }
        metric_columns = [
            column for column in METRICS if column in result.columns
        ]
        record = record_optimization_experiment(
            df,
            strategy_name=strategy_cls.strategy_key,
            parameter_ranges=param_grid,
            metric=metric,
            method="core_grid",
            output={
                "best_params": best_params,
                "best_value": float(best_row[metric]),
                "best_metrics": {
                    key: float(best_row[key]) for key in metric_columns
                },
                "results": result,
            },
            costs=CostAssumptions(
                init_cash=engine_kwargs.get("init_cash", 1_000_000),
                commission=engine_kwargs.get("commission", 0.00025),
                commission_min=engine_kwargs.get("commission_min", 5.0),
                slippage=engine_kwargs.get("slippage", 0.001),
                impact_coefficient=engine_kwargs.get("impact_coefficient", 0.02),
                max_slippage=engine_kwargs.get("max_slippage", 0.05),
                participation_rate=engine_kwargs.get("participation_rate", 0.05),
                lot_size=engine_kwargs.get("trade_unit", 100),
                stamp_tax=engine_kwargs.get("stamp_tax", True),
                transfer_fee=engine_kwargs.get("transfer_fee", True),
                t_plus_1=engine_kwargs.get("t_plus_1", True),
                price_limit=engine_kwargs.get("price_limit", True),
                limit_queue_fill_ratio=engine_kwargs.get(
                    "limit_queue_fill_ratio", 0.25
                ),
                order_ttl_bars=engine_kwargs.get("order_ttl_bars", 5),
                execution_model=engine_kwargs.get(
                    "execution_model", "realistic"
                ),
                rf=engine_kwargs.get("rf", 0.0),
            ),
            code=engine_kwargs.get("code"),
            name=experiment_name,
            store=experiment_store,
        )
        out["experiment_id"] = record.experiment_id
        out["reproducibility_key"] = record.reproducibility_key
        out["result_hash"] = record.result_hash
    return out


def build_heatmap(results, x_param: str, y_param: str, metric: str) -> go.Figure:
    """二维参数热力图（恰好两个参数时使用）

    参数:
        results: grid_search/optimize 返回的结果表
        x_param: 横轴参数名（如 "fast"）
        y_param: 纵轴参数名（如 "slow"）
        metric:  展示的指标
    """
    pivot = results.pivot(index=y_param, columns=x_param, values=metric)
    fmt = METRIC_FMT.get(metric, ".2f")
    fig = go.Figure(go.Heatmap(
        z=pivot.values, x=pivot.columns, y=pivot.index,
        colorscale="Blues",
        colorbar=dict(title=dict(text=metric, font=dict(color="#52514e", size=11))),
        texttemplate=f"%{{z:{fmt}}}", textfont=dict(color="#898781", size=11),
        hovertemplate=(
            f"{y_param}=%{{y}}，{x_param}=%{{x}}<br>{metric}：%{{z:{fmt}}}<extra></extra>"
        ),
    ))
    fig.update_layout(
        title=dict(text=f"参数热力图（{metric}）", font=dict(size=16, color="#0b0b0b")),
        xaxis=dict(title=dict(text=x_param, font=dict(color="#52514e")),
                   type="category", tickfont=dict(color="#898781")),
        yaxis=dict(title=dict(text=y_param, font=dict(color="#52514e")),
                   type="category", tickfont=dict(color="#898781")),
        paper_bgcolor="#fcfcfb",
        plot_bgcolor="#fcfcfb",
        font=dict(color="#52514e", size=12),
        margin=dict(l=40, r=20, t=50, b=40),
        height=400,
    )
    return fig


def run_test():
    """测试：网格寻优 + 热力图（贵州茅台近 3 年，双均线策略小网格）

    运行方式:
        python core/optimizer.py
    """
    import datetime

    from strategies.double_ma import DoubleMAStrategy
    from utils.data_loader import load_daily_data

    print("===== 参数寻优测试开始 =====")
    end = datetime.date.today()
    start = end - datetime.timedelta(days=3 * 365)
    df = load_daily_data("600519", start, end)

    # 1. 小网格寻优（2×2 = 4 组）
    out = optimize(df, DoubleMAStrategy, {"fast": [5, 10], "slow": [20, 60]},
                   metric="夏普比率", engine_kwargs={"init_cash": 1_000_000})
    results = out["results"]
    assert len(results) == 4, f"应有 4 行结果，实际 {len(results)}"
    assert out["skipped"] == 0
    assert list(results.columns[:2]) == ["fast", "slow"]
    assert abs(out["best"]["夏普比率"] - results["夏普比率"].max()) < 1e-9, "最优行应排第一"
    print("寻优结果（按夏普比率排序）：")
    print(results.to_string(index=False,
                            float_format=lambda x: f"{x:.4f}".rstrip("0")))
    print(f"  ✓ 网格寻优：4 组全有效，最优 fast={int(out['best']['fast'])}、"
          f"slow={int(out['best']['slow'])}，夏普 {out['best']['夏普比率']:.4f}")

    # 2. 非法组合自动跳过（fast>=slow 的 2 组）
    out2 = optimize(df, DoubleMAStrategy, {"fast": [5, 20], "slow": [10, 20]},
                    metric="累计收益率")
    assert len(out2["results"]) == 2 and out2["skipped"] == 2
    print("  ✓ 非法组合自动跳过：2 组有效 + 2 组跳过（fast>=slow）")

    # 3. 最大回撤按从小到大排
    out3 = optimize(df, DoubleMAStrategy, {"fast": [5, 10], "slow": [20, 60]},
                    metric="最大回撤")
    dd = out3["results"]["最大回撤"].values
    assert (dd[1:] >= dd[:-1]).all(), "最大回撤应按从小到大排序"
    print("  ✓ 最大回撤寻优：结果按回撤从小到大排列")

    # 4. 热力图
    fig = build_heatmap(out["results"], "fast", "slow", "夏普比率")
    assert isinstance(fig, go.Figure) and isinstance(fig.data[0], go.Heatmap)
    assert len(fig.data[0].x) == 2 and len(fig.data[0].y) == 2
    print("  ✓ 热力图：2×2 网格生成成功")

    # 5. ranges_to_grid（步长序列）
    assert ranges_to_grid({"fast": {"min": 5, "max": 15, "step": 5}}) == {"fast": [5, 10, 15]}
    assert ranges_to_grid({"k": {"min": 1.0, "max": 2.0, "step": 0.5}}) == {"k": [1.0, 1.5, 2.0]}
    try:
        ranges_to_grid({"fast": {"min": 10, "max": 5, "step": 1}})
    except ValueError:
        pass
    else:
        raise AssertionError("非法范围应报错")
    print("  ✓ ranges_to_grid：整数/浮点步长序列正确，非法范围报错")

    # 6. 组合数超限报错（上限 200）
    big_grid = {f"p{i}": list(range(20)) for i in range(3)}  # 8000 组
    try:
        optimize(df, DoubleMAStrategy, big_grid, metric="夏普比率")
    except ValueError as e:
        print(f"  ✓ 组合数超限报错：{e}")
    else:
        raise AssertionError("超过 200 组的网格应报错")

    print("===== 全部测试通过 =====")


if __name__ == "__main__":
    run_test()
