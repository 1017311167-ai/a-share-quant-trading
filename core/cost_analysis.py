"""交易成本敏感性分析。"""

from __future__ import annotations

import pandas as pd

from core.backtest_engine import BacktestEngine


def cost_sensitivity_analysis(
        df,
        entries,
        exits,
        *,
        base_kwargs=None,
        commission_multipliers=(0.5, 1.0, 2.0, 3.0),
        slippage_values=None,
        participation_rates=None,
        min_commission_values=None,
) -> pd.DataFrame:
    """运行一组成本场景并返回敏感性结果。

    默认执行一因子扰动：
    - 佣金费率倍数。
    - 基础滑点。
    - 单根 K 线成交量参与率。
    - 每笔最低佣金。

    同时包含基准场景和零交易成本场景。
    """
    base = {
        "init_cash": 1_000_000,
        "commission": 0.00025,
        "commission_min": 5.0,
        "slippage": 0.001,
        "impact_coefficient": 0.02,
        "max_slippage": 0.05,
        "participation_rate": 0.05,
        "trade_unit": 100,
        "rf": 0.0,
        "code": None,
        "t_plus_1": True,
        "price_limit": True,
        "stamp_tax": True,
        "transfer_fee": True,
        "limit_queue_fill_ratio": 0.25,
        "order_ttl_bars": 5,
        "execution_model": "realistic",
    }
    base.update(base_kwargs or {})
    if base.get("execution_model") != "realistic":
        raise ValueError("成本敏感性分析只支持 realistic 成交模型")
    slippage_values = tuple(
        slippage_values
        if slippage_values is not None
        else (base["slippage"] * 0.5, base["slippage"],
              base["slippage"] * 2, base["slippage"] * 3)
    )
    participation_rates = tuple(
        participation_rates
        if participation_rates is not None
        else (0.01, 0.05, 0.1, 0.2)
    )
    min_commission_values = tuple(
        min_commission_values
        if min_commission_values is not None
        else (0.0, base["commission_min"], base["commission_min"] * 2)
    )

    scenarios = [
        ("基准", dict(base)),
        ("零交易成本", {
            **base,
            "commission": 0.0,
            "commission_min": 0.0,
            "slippage": 0.0,
            "impact_coefficient": 0.0,
            "stamp_tax": False,
            "transfer_fee": False,
        }),
    ]
    seen = {
        _scenario_key(name, params)
        for name, params in scenarios
    }
    for factor in commission_multipliers:
        params = {
            **base,
            "commission": base["commission"] * float(factor),
        }
        _append_scenario(scenarios, seen, f"佣金 x{factor:g}", params)
    for value in slippage_values:
        params = {**base, "slippage": float(value)}
        _append_scenario(scenarios, seen, f"滑点 {value:.4%}", params)
    for value in participation_rates:
        params = {**base, "participation_rate": float(value)}
        _append_scenario(scenarios, seen, f"参与率 {value:.2%}", params)
    for value in min_commission_values:
        params = {**base, "commission_min": float(value)}
        _append_scenario(
            scenarios, seen, f"最低佣金 {value:.2f} 元", params
        )

    rows = []
    for name, params in scenarios:
        engine = BacktestEngine(df, entries, exits, **params).run()
        metrics = engine.get_metrics()
        execution = engine.get_execution_stats()
        orders = engine.get_orders()
        requested = float(orders["requested"].fillna(0).sum()) if len(orders) else 0.0
        filled = float(orders["filled"].fillna(0).sum()) if len(orders) else 0.0
        rows.append({
            "场景": name,
            "佣金费率": params["commission"],
            "最低佣金": params["commission_min"],
            "基础滑点": params["slippage"],
            "冲击成本系数": params["impact_coefficient"],
            "成交量参与率": params["participation_rate"],
            "累计收益率": metrics["累计收益率"],
            "年化收益率": metrics["年化收益率"],
            "夏普比率": metrics["夏普比率"],
            "最大回撤": metrics["最大回撤"],
            "总手续费": metrics["总手续费"],
            "成交笔数": metrics["成交笔数"],
            "部分成交订单数": metrics["部分成交订单数"],
            "成交率": filled / requested if requested > 0 else 0.0,
            "平均滑点": metrics["平均滑点"],
        })

    report = pd.DataFrame(rows)
    baseline_return = float(report.loc[report["场景"] == "基准", "累计收益率"].iloc[0])
    zero_cost_return = float(
        report.loc[report["场景"] == "零交易成本", "累计收益率"].iloc[0]
    )
    report["相对基准收益变化"] = report["累计收益率"] - baseline_return
    report["相对零成本拖累"] = zero_cost_return - report["累计收益率"]
    return report


def summarize_cost_sensitivity(report: pd.DataFrame) -> dict:
    """提取成本敏感性报告的关键结论。"""
    if report is None or report.empty:
        raise ValueError("成本敏感性报告为空")
    required = {"场景", "累计收益率", "总手续费", "相对零成本拖累"}
    missing = required - set(report.columns)
    if missing:
        raise ValueError(f"成本敏感性报告缺少列：{sorted(missing)}")
    worst = report.loc[report["累计收益率"].idxmin()]
    baseline = report.loc[report["场景"] == "基准"]
    zero = report.loc[report["场景"] == "零交易成本"]
    return {
        "基准收益率": float(baseline["累计收益率"].iloc[0]),
        "零成本收益率": float(zero["累计收益率"].iloc[0]),
        "基准成本拖累": float(baseline["相对零成本拖累"].iloc[0]),
        "最差场景": str(worst["场景"]),
        "最差收益率": float(worst["累计收益率"]),
        "最差场景总费用": float(worst["总手续费"]),
    }


def cost_sensitivity_from_strategy(df, strategy, params=None,
                                   **kwargs) -> pd.DataFrame:
    """从策略对象或策略名直接运行成本敏感性分析。"""
    if isinstance(strategy, str):
        from strategies.factory import create_strategy
        strategy = create_strategy(strategy, **(params or {}))
    signal = strategy.generate_signal_output(df)
    return cost_sensitivity_analysis(
        df, signal.entries, signal.exits, **kwargs
    )


def _append_scenario(scenarios, seen, name, params):
    key = _scenario_key(name, params)
    if key in seen:
        return
    seen.add(key)
    scenarios.append((name, params))


def _scenario_key(name, params):
    return (
        float(params["commission"]),
        float(params["commission_min"]),
        float(params["slippage"]),
        float(params["participation_rate"]),
        bool(params["stamp_tax"]),
        bool(params["transfer_fee"]),
    )
