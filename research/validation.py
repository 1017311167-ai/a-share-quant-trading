"""策略稳健性、Walk-forward 和过拟合风险评估。"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from core.backtest_engine import BacktestEngine
from optimization import optimize_parameters
from strategies.factory import create_strategy
from utils.versioning import get_code_version


MINIMIZE_METRICS = {"最大回撤", "年化波动率", "总手续费"}


@dataclass(frozen=True)
class ValidationConfig:
    """稳健性评估配置。"""

    in_sample_ratio: float = 0.7
    n_splits: int = 4
    train_ratio: float = 0.5
    window_mode: str = "anchored"
    monte_carlo_runs: int = 500
    monte_carlo_block_size: int | None = None
    monte_carlo_seed: int = 42
    top_k: int = 10
    min_train_bars: int = 60
    min_test_bars: int = 20

    def __post_init__(self):
        if not 0 < self.in_sample_ratio < 1:
            raise ValueError("in_sample_ratio 必须在 (0, 1) 之间")
        if self.n_splits < 2:
            raise ValueError("n_splits 必须至少为 2")
        if not 0 < self.train_ratio < 1:
            raise ValueError("train_ratio 必须在 (0, 1) 之间")
        if self.window_mode not in {"anchored", "rolling"}:
            raise ValueError("window_mode 只支持 anchored 或 rolling")
        if self.monte_carlo_runs < 50:
            raise ValueError("蒙特卡洛次数至少为 50")
        if self.top_k < 1:
            raise ValueError("top_k 必须为正整数")


@dataclass
class RobustnessReport:
    """单策略完整稳健性报告。"""

    strategy: dict
    data: dict
    metric: str
    split: dict
    selected_params: dict
    in_sample: dict
    out_sample: dict
    rolling: dict
    walk_forward: dict
    parameter_stability: dict
    monte_carlo: dict
    benchmark: dict
    overfitting: dict
    warnings: list[str] = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )

    def to_dict(self) -> dict:
        return _jsonable(asdict(self))

    def summary_row(self) -> dict:
        """返回策略横向对比的一行摘要。"""
        return {
            "策略": self.strategy.get("name"),
            "策略版本": self.strategy.get("version"),
            "实现哈希": self.strategy.get("implementation_hash"),
            "最优参数": self.selected_params,
            "样本内指标": self.in_sample.get("metric_value"),
            "样本外指标": self.out_sample.get("metric_value"),
            "样本外收益": self.out_sample.get("metrics", {}).get("累计收益率"),
            "Walk-forward收益": self.walk_forward.get("metrics", {}).get(
                "累计收益率"
            ),
            "Walk-forward正收益折数": self.walk_forward.get(
                "positive_fold_ratio"
            ),
            "参数稳定性": self.parameter_stability.get("stability_score"),
            "蒙特卡洛正收益概率": self.monte_carlo.get("probability_positive"),
            "基准超额收益": self.benchmark.get("excess_return"),
            "过拟合风险": self.overfitting.get("risk_level"),
            "稳健性分数": self.overfitting.get("robustness_score"),
            "主要风险": "；".join(self.overfitting.get("reasons", [])),
        }


def evaluate_strategy_robustness(
        df: pd.DataFrame,
        strategy_name: str,
        param_ranges: dict,
        *,
        metric: str = "夏普比率",
        method: str = "grid",
        config: ValidationConfig | None = None,
        optimizer_kwargs: dict | None = None,
        code: str | None = None,
) -> RobustnessReport:
    """执行样本内外、滚动、Walk-forward、参数稳定性和蒙特卡洛评估。"""
    config = config or ValidationConfig()
    df = _prepare_validation_data(df)
    if df is None or len(df) < config.min_train_bars:
        raise ValueError("稳健性评估需要足够长的行情数据")
    optimizer_kwargs = dict(optimizer_kwargs or {})
    optimizer_kwargs.setdefault("verbose", False)
    optimizer_kwargs.setdefault("max_workers", 1)
    optimizer_kwargs.setdefault("record_experiment", False)
    if code is not None:
        optimizer_kwargs.setdefault("code", code)

    strategy = create_strategy(strategy_name)
    metadata = strategy.metadata()
    in_sample_df, out_sample_df = split_in_out_sample(
        df, config.in_sample_ratio, config.min_train_bars, config.min_test_bars
    )

    in_optimization = optimize_parameters(
        in_sample_df,
        strategy_name,
        param_ranges,
        metric=metric,
        method=method,
        **optimizer_kwargs,
    )
    selected_params = dict(in_optimization["best_params"])
    in_result = evaluate_params_on_frame(
        in_sample_df, strategy_name, selected_params,
        metric=metric, engine_kwargs=_engine_kwargs_from_optimizer(
            optimizer_kwargs, code
        ),
    )
    out_result = evaluate_params_on_frame(
        out_sample_df, strategy_name, selected_params,
        metric=metric, engine_kwargs=_engine_kwargs_from_optimizer(
            optimizer_kwargs, code
        ),
    )
    rolling = rolling_backtest(
        df,
        strategy_name,
        selected_params,
        metric=metric,
        config=config,
        engine_kwargs=_engine_kwargs_from_optimizer(optimizer_kwargs, code),
    )
    walk_forward = walk_forward_validate(
        df,
        strategy_name,
        param_ranges,
        metric=metric,
        method=method,
        config=config,
        optimizer_kwargs=optimizer_kwargs,
    )
    stability = parameter_stability(
        in_optimization["results"], metric, top_k=config.top_k
    )
    monte_carlo_returns = (
        walk_forward["returns"]
        if len(walk_forward.get("returns", pd.Series(dtype=float))) >= 20
        else out_result["returns"]
    )
    monte_carlo = monte_carlo_validation(
        monte_carlo_returns,
        runs=config.monte_carlo_runs,
        seed=config.monte_carlo_seed,
        block_size=config.monte_carlo_block_size,
    )
    benchmark = compare_with_buy_and_hold(
        out_result["returns"],
        out_sample_df["close"],
    )
    overfitting = assess_overfitting_risk(
        metric=metric,
        in_metrics=in_result["metrics"],
        out_metrics=out_result["metrics"],
        walk_forward=walk_forward,
        parameter_stability=stability,
        monte_carlo=monte_carlo,
        benchmark=benchmark,
    )
    warnings = []
    warnings.extend(walk_forward.get("failed", []))
    if out_result["metrics"].get("总交易次数", 0) < 5:
        warnings.append("样本外完整交易次数少于 5，结论不稳定")
    return RobustnessReport(
        strategy=metadata.to_dict(),
        data={
            "symbol": _infer_symbol(df, code),
            "data_version": df.attrs.get("data_version", "unknown"),
            "start": str(df.index.min()),
            "end": str(df.index.max()),
            "rows": len(df),
        },
        metric=metric,
        split={
            "in_sample_rows": len(in_sample_df),
            "out_sample_rows": len(out_sample_df),
            "in_sample_start": str(in_sample_df.index.min()),
            "in_sample_end": str(in_sample_df.index.max()),
            "out_sample_start": str(out_sample_df.index.min()),
            "out_sample_end": str(out_sample_df.index.max()),
        },
        selected_params=selected_params,
        in_sample=_report_section(in_result),
        out_sample=_report_section(out_result),
        rolling=_report_section(rolling),
        walk_forward=_report_section(walk_forward),
        parameter_stability=stability,
        monte_carlo=monte_carlo,
        benchmark=benchmark,
        overfitting=overfitting,
        warnings=warnings,
    )


def compare_strategy_robustness(
        df: pd.DataFrame,
        strategies: list[dict],
        *,
        config: ValidationConfig | None = None,
        method: str = "grid",
        optimizer_kwargs: dict | None = None,
) -> pd.DataFrame:
    """批量评估多个策略并返回过拟合风险对比表。"""
    rows = []
    for item in strategies:
        try:
            report = evaluate_strategy_robustness(
                df,
                item["name"],
                item["param_ranges"],
                metric=item.get("metric", "夏普比率"),
                method=item.get("method", method),
                config=config,
                optimizer_kwargs={
                    **(optimizer_kwargs or {}),
                    **item.get("optimizer_kwargs", {}),
                },
                code=item.get("code"),
            )
            rows.append(report.summary_row())
        except Exception as exc:
            rows.append({
                "策略": item.get("name"),
                "过拟合风险": "评估失败",
                "稳健性分数": None,
                "主要风险": f"{type(exc).__name__}: {exc}",
            })
    return pd.DataFrame(rows)


def split_in_out_sample(df: pd.DataFrame, ratio: float,
                        min_train_bars: int = 60,
                        min_test_bars: int = 20):
    """按时间顺序划分样本内和样本外。"""
    if df is None or len(df) < min_train_bars + min_test_bars:
        raise ValueError("数据不足以划分样本内和样本外")
    split = int(len(df) * ratio)
    split = max(min_train_bars, min(split, len(df) - min_test_bars))
    return _slice_frame(df, 0, split), _slice_frame(df, split, len(df))


def rolling_backtest(
        df: pd.DataFrame,
        strategy_name: str,
        params: dict,
        *,
        metric: str = "夏普比率",
        config: ValidationConfig | None = None,
        engine_kwargs: dict | None = None,
) -> dict:
    """固定参数在连续滚动测试窗口运行。"""
    config = config or ValidationConfig()
    df = _prepare_validation_data(df)
    folds = []
    returns_parts = []
    for number, (train_slice, test_slice) in enumerate(
            _walk_forward_windows(df, config), start=1
    ):
        result = evaluate_params_on_frame(
            test_slice,
            strategy_name,
            params,
            metric=metric,
            engine_kwargs=engine_kwargs,
        )
        returns_parts.append(result["returns"])
        folds.append({
            "fold": number,
            "test_start": str(test_slice.index.min()),
            "test_end": str(test_slice.index.max()),
            "返回": result["metrics"]["累计收益率"],
            "夏普": result["metrics"].get("夏普比率"),
            "最大回撤": result["metrics"].get("最大回撤"),
            "交易次数": result["metrics"].get("总交易次数"),
        })
    return _aggregate_fold_results(folds, returns_parts)


def walk_forward_validate(
        df: pd.DataFrame,
        strategy_name: str,
        param_ranges: dict,
        *,
        metric: str = "夏普比率",
        method: str = "grid",
        config: ValidationConfig | None = None,
        optimizer_kwargs: dict | None = None,
) -> dict:
    """逐窗训练选参，再在紧随其后的测试窗口评估。"""
    config = config or ValidationConfig()
    df = _prepare_validation_data(df)
    optimizer_kwargs = dict(optimizer_kwargs or {})
    optimizer_kwargs.setdefault("verbose", False)
    optimizer_kwargs.setdefault("max_workers", 1)
    optimizer_kwargs.setdefault("record_experiment", False)
    folds, returns_parts, failed = [], [], []
    parameter_history = []
    for number, (train_slice, test_slice) in enumerate(
            _walk_forward_windows(df, config), start=1
    ):
        try:
            optimized = optimize_parameters(
                train_slice,
                strategy_name,
                param_ranges,
                metric=metric,
                method=method,
                **optimizer_kwargs,
            )
            params = dict(optimized["best_params"])
            test_result = evaluate_params_on_frame(
                test_slice,
                strategy_name,
                params,
                metric=metric,
                engine_kwargs=_engine_kwargs_from_optimizer(
                    optimizer_kwargs, optimizer_kwargs.get("code")
                ),
            )
            benchmark_return = _buy_hold_return(test_slice["close"])
            returns_parts.append(test_result["returns"])
            parameter_history.append(params)
            folds.append({
                "fold": number,
                "train_start": str(train_slice.index.min()),
                "train_end": str(train_slice.index.max()),
                "test_start": str(test_slice.index.min()),
                "test_end": str(test_slice.index.max()),
                "params": params,
                "train_best": optimized["best_value"],
                "test_metric": test_result["metric_value"],
                "test_return": test_result["metrics"]["累计收益率"],
                "benchmark_return": benchmark_return,
            })
        except Exception as exc:
            failed.append(f"第 {number} 折失败：{type(exc).__name__}: {exc}")
    aggregate = _aggregate_fold_results(folds, returns_parts)
    aggregate["failed"] = failed
    aggregate["parameter_history"] = parameter_history
    return aggregate


def evaluate_params_on_frame(
        df: pd.DataFrame,
        strategy_name: str,
        params: dict,
        *,
        metric: str = "夏普比率",
        engine_kwargs: dict | None = None,
) -> dict:
    """在指定时间切片运行参数并返回指标和收益序列。"""
    df = _prepare_validation_data(df)
    strategy = create_strategy(strategy_name, **params)
    signal = strategy.generate_signal_output(df)
    engine = BacktestEngine(
        df,
        signal.entries,
        signal.exits,
        **dict(engine_kwargs or {}),
    ).run()
    metrics = engine.get_metrics()
    returns = engine.get_equity().pct_change().dropna()
    return {
        "params": strategy.params(),
        "metrics": _jsonable(metrics),
        "metric_value": _safe_float(metrics.get(metric)),
        "returns": returns,
        "equity": engine.get_equity(),
    }


def parameter_stability(results: pd.DataFrame, metric: str, *,
                        top_k: int = 10) -> dict:
    """检查最优参数附近是否具有连续、稳定的绩效。"""
    if metric not in results.columns:
        raise ValueError(f"参数寻优结果缺少指标：{metric}")
    if results.empty:
        raise ValueError("参数寻优结果为空")
    top = results.head(min(top_k, len(results))).copy()
    values = pd.to_numeric(top[metric], errors="coerce").dropna()
    if values.empty:
        return {
            "stability_score": 0.0,
            "performance_retention": 0.0,
            "parameter_dispersion": 1.0,
            "top_params": [],
        }
    best_value = float(values.iloc[0])
    median_value = float(values.median())
    scale = max(abs(best_value), abs(median_value), 1e-9)
    retention = 1.0 - min(abs(best_value - median_value) / scale, 1.0)
    metric_columns = {
        "累计收益率", "年化收益率", "年化波动率", "夏普比率",
        "卡玛比率", "最大回撤", "胜率", "盈亏比", "总交易次数",
        "总手续费", "期末总资产", "基准收益率",
    }
    parameter_columns = [
        column for column in top.columns if column not in metric_columns
    ]
    dispersion_values = []
    for column in parameter_columns:
        series = pd.to_numeric(top[column], errors="coerce").dropna()
        if len(series) < 2:
            continue
        span = float(results[column].max() - results[column].min())
        if span > 0:
            dispersion_values.append(float(series.std(ddof=0) / span))
    dispersion = float(np.mean(dispersion_values)) if dispersion_values else 0.0
    dispersion_score = 1.0 - min(max(dispersion, 0.0), 1.0)
    score = 0.6 * retention + 0.4 * dispersion_score
    return {
        "stability_score": float(np.clip(score, 0.0, 1.0)),
        "performance_retention": float(np.clip(retention, 0.0, 1.0)),
        "parameter_dispersion": dispersion,
        "best_value": best_value,
        "top_k_median": median_value,
        "top_params": _jsonable(top.to_dict(orient="records")),
    }


def monte_carlo_validation(returns: pd.Series, *, runs: int = 500,
                           seed: int = 42,
                           block_size: int | None = None) -> dict:
    """对收益序列执行固定块 bootstrap。"""
    values = pd.Series(returns).dropna().to_numpy(dtype=float)
    if len(values) == 0:
        return {
            "runs": 0,
            "probability_positive": 0.0,
            "probability_drawdown_over_20": 0.0,
            "terminal_return": {},
            "maximum_drawdown": {},
        }
    block_size = block_size or max(2, int(math.sqrt(len(values))))
    block_size = min(max(1, block_size), len(values))
    rng = np.random.default_rng(seed)
    paths = np.empty((runs, len(values)), dtype=float)
    block_count = math.ceil(len(values) / block_size)
    for run in range(runs):
        starts = rng.integers(
            0, max(1, len(values) - block_size + 1), size=block_count
        )
        sampled = np.concatenate([
            values[start:start + block_size] for start in starts
        ])[:len(values)]
        paths[run] = sampled
    cumulative = np.cumprod(1 + paths, axis=1)
    terminal = cumulative[:, -1] - 1
    running_max = np.maximum.accumulate(cumulative, axis=1)
    drawdowns = cumulative / running_max - 1
    max_drawdowns = np.abs(drawdowns.min(axis=1))
    return {
        "runs": runs,
        "block_size": block_size,
        "probability_positive": float((terminal > 0).mean()),
        "probability_drawdown_over_20": float((max_drawdowns > 0.2).mean()),
        "terminal_return": _distribution(terminal),
        "maximum_drawdown": _distribution(max_drawdowns),
    }


def compare_with_buy_and_hold(returns: pd.Series,
                              close: pd.Series) -> dict:
    """比较策略与同期买入持有。"""
    strategy_returns = pd.Series(returns).dropna()
    benchmark_returns = pd.Series(close).pct_change().reindex(
        strategy_returns.index
    ).dropna()
    common = strategy_returns.index.intersection(benchmark_returns.index)
    strategy_returns = strategy_returns.reindex(common)
    benchmark_returns = benchmark_returns.reindex(common)
    strategy_metrics = _metrics_from_returns(strategy_returns, common)
    benchmark_metrics = _metrics_from_returns(benchmark_returns, common)
    excess = (
        strategy_metrics["累计收益率"] - benchmark_metrics["累计收益率"]
    )
    return {
        "strategy": strategy_metrics,
        "buy_and_hold": benchmark_metrics,
        "excess_return": float(excess),
        "beat_benchmark": bool(excess > 0),
    }


def assess_overfitting_risk(*, metric: str, in_metrics: dict,
                            out_metrics: dict, walk_forward: dict,
                            parameter_stability: dict,
                            monte_carlo: dict,
                            benchmark: dict) -> dict:
    """综合多个证据输出过拟合风险和稳健性分数。"""
    score = 100.0
    reasons = []
    direction = -1.0 if metric in MINIMIZE_METRICS else 1.0
    in_value = _safe_float(in_metrics.get(metric), 0.0) * direction
    out_value = _safe_float(out_metrics.get(metric), 0.0) * direction
    scale = max(abs(in_value), abs(out_value), 1e-9)
    degradation = (in_value - out_value) / scale
    if degradation > 0:
        penalty = min(35.0, degradation * 45.0)
        score -= penalty
        reasons.append(f"样本外指标下降 {degradation:.1%}")

    positive_ratio = _safe_float(
        walk_forward.get("positive_fold_ratio"), 0.0
    )
    if positive_ratio < 0.6:
        penalty = min(25.0, (0.6 - positive_ratio) * 50.0)
        score -= penalty
        reasons.append(f"Walk-forward 正收益折数仅 {positive_ratio:.0%}")

    beat_ratio = _safe_float(
        walk_forward.get("beat_benchmark_ratio"), 0.0
    )
    if beat_ratio < 0.5:
        penalty = min(15.0, (0.5 - beat_ratio) * 30.0)
        score -= penalty
        reasons.append(f"仅 {beat_ratio:.0%} 的 Walk-forward 折跑赢基准")

    stability = _safe_float(
        parameter_stability.get("stability_score"), 0.0
    )
    if stability < 0.6:
        penalty = min(20.0, (0.6 - stability) * 40.0)
        score -= penalty
        reasons.append(f"参数稳定性偏低：{stability:.2f}")

    probability_positive = _safe_float(
        monte_carlo.get("probability_positive"), 0.0
    )
    if probability_positive < 0.6:
        penalty = min(15.0, (0.6 - probability_positive) * 35.0)
        score -= penalty
        reasons.append(f"蒙特卡洛正收益概率仅 {probability_positive:.0%}")

    if not benchmark.get("beat_benchmark", False):
        score -= 10.0
        reasons.append("样本外未跑赢买入持有基准")

    if _safe_float(out_metrics.get("总交易次数"), 0.0) < 5:
        score -= 10.0
        reasons.append("样本外交易样本少于 5 次")

    score = float(np.clip(score, 0.0, 100.0))
    if score >= 75:
        risk_level = "LOW"
    elif score >= 50:
        risk_level = "MEDIUM"
    else:
        risk_level = "HIGH"
    return {
        "robustness_score": score,
        "risk_level": risk_level,
        "in_sample_objective": in_value,
        "out_sample_objective": out_value,
        "degradation": float(degradation),
        "reasons": reasons or ["未发现显著过拟合证据"],
    }


def _walk_forward_windows(df: pd.DataFrame, config: ValidationConfig):
    n = len(df)
    train_bars = max(config.min_train_bars, int(n * config.train_ratio))
    remaining = n - train_bars
    if remaining < config.min_test_bars:
        raise ValueError("数据长度不足以生成 Walk-forward 测试窗口")
    test_bars = max(config.min_test_bars, remaining // config.n_splits)
    windows = []
    for i in range(config.n_splits):
        test_start = train_bars + i * test_bars
        if test_start >= n:
            break
        test_end = min(test_start + test_bars, n)
        if test_end - test_start < config.min_test_bars:
            break
        if config.window_mode == "anchored":
            train_start = 0
        else:
            train_start = max(0, test_start - train_bars)
        windows.append((
            _slice_frame(df, train_start, test_start),
            _slice_frame(df, test_start, test_end),
        ))
    if not windows:
        raise ValueError("无法生成 Walk-forward 窗口")
    return windows


def _aggregate_fold_results(folds: list[dict],
                            returns_parts: list[pd.Series]) -> dict:
    if returns_parts:
        returns = pd.concat(returns_parts).sort_index()
        returns = returns[~returns.index.duplicated(keep="first")]
    else:
        returns = pd.Series(dtype=float)
    equity = (1 + returns).cumprod() if len(returns) else pd.Series(dtype=float)
    metrics = _metrics_from_returns(returns, returns.index)
    positive = sum(1 for fold in folds if _fold_return(fold) > 0)
    beat = sum(
        1 for fold in folds
        if _fold_return(fold) > _safe_float(
            fold.get("benchmark_return"), 0.0
        )
    )
    return {
        "folds": _jsonable(folds),
        "fold_count": len(folds),
        "positive_fold_ratio": positive / len(folds) if folds else 0.0,
        "beat_benchmark_ratio": beat / len(folds) if folds else 0.0,
        "metrics": metrics,
        "returns": returns,
        "equity": equity,
    }


def _metrics_from_returns(returns: pd.Series, index) -> dict:
    returns = pd.Series(returns).dropna()
    if returns.empty:
        return {
            "累计收益率": 0.0,
            "年化收益率": 0.0,
            "年化波动率": 0.0,
            "夏普比率": 0.0,
            "最大回撤": 0.0,
        }
    cumulative = (1 + returns).cumprod()
    total_return = float(cumulative.iloc[-1] - 1)
    dt_index = pd.DatetimeIndex(index)
    n_days = (dt_index[-1] - dt_index[0]).days if len(dt_index) > 1 else 0
    annual_return = (
        (1 + total_return) ** (365.25 / n_days) - 1
        if total_return > -1 and n_days > 0 else 0.0
    )
    ppy = _periods_per_year(dt_index)
    volatility = float(returns.std(ddof=1) * math.sqrt(ppy)) \
        if len(returns) > 1 else 0.0
    sharpe = float(
        returns.mean() / returns.std(ddof=1) * math.sqrt(ppy)
    ) if len(returns) > 1 and returns.std(ddof=1) > 0 else 0.0
    drawdown = cumulative / cumulative.cummax() - 1
    return {
        "累计收益率": total_return,
        "年化收益率": float(annual_return),
        "年化波动率": volatility,
        "夏普比率": sharpe,
        "最大回撤": float(abs(drawdown.min())),
    }


def _periods_per_year(index: pd.DatetimeIndex) -> float:
    if len(index) < 2:
        return 252.0
    seconds = float((index[1:] - index[:-1]).median().total_seconds())
    if seconds >= 86400:
        return 252.0
    bars_per_day = len(index) / max(index.normalize().nunique(), 1)
    return max(252.0 * bars_per_day, 252.0)


def _engine_kwargs_from_optimizer(optimizer_kwargs: dict,
                                  code: str | None) -> dict:
    allowed = {
        "init_cash", "commission", "commission_min", "slippage",
        "impact_coefficient", "max_slippage", "participation_rate",
        "trade_unit", "rf", "t_plus_1", "price_limit", "stamp_tax",
        "transfer_fee", "limit_queue_fill_ratio", "order_ttl_bars",
        "execution_model",
    }
    result = {
        key: value for key, value in optimizer_kwargs.items()
        if key in allowed
    }
    if code is not None:
        result["code"] = code
    elif optimizer_kwargs.get("code") is not None:
        result["code"] = optimizer_kwargs["code"]
    return result


def _slice_frame(df: pd.DataFrame, start: int, end: int) -> pd.DataFrame:
    result = df.iloc[start:end].copy()
    result.attrs.update(df.attrs)
    return result


def _prepare_validation_data(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) == 0:
        raise ValueError("验证行情数据为空")
    result = df.copy()
    if "date" in result.columns:
        result["date"] = pd.to_datetime(result["date"])
        result = result.set_index("date")
    if not isinstance(result.index, pd.DatetimeIndex):
        raise ValueError("验证行情需要日期索引或 date 列")
    result = result.sort_index()
    result = result[~result.index.duplicated(keep="last")]
    return result


def _buy_hold_return(close: pd.Series) -> float:
    close = pd.Series(close).dropna()
    return float(close.iloc[-1] / close.iloc[0] - 1) if len(close) > 1 else 0.0


def _fold_return(fold: dict) -> float:
    return _safe_float(
        fold.get("test_return", fold.get("返回")), 0.0
    )


def _distribution(values) -> dict:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if len(values) == 0:
        return {}
    return {
        "p05": float(np.quantile(values, 0.05)),
        "p25": float(np.quantile(values, 0.25)),
        "median": float(np.median(values)),
        "p75": float(np.quantile(values, 0.75)),
        "p95": float(np.quantile(values, 0.95)),
    }


def _safe_float(value, default=None):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _infer_symbol(df: pd.DataFrame, code: str | None) -> str:
    manifest = df.attrs.get("snapshot_manifest", {})
    return code or manifest.get("symbol") or "unknown"


def _report_section(data: dict) -> dict:
    return {
        key: value for key, value in data.items()
        if key not in {"returns", "equity"}
    }


def _jsonable(value):
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if pd.isna(value) if not isinstance(value, (dict, list, tuple)) else False:
        return None
    return value
