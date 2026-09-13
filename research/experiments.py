"""可复现的实验记录与回放。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from core.backtest_engine import BacktestEngine
from core.risk_analysis import analyze_portfolio
from data.models import Frequency
from data.service import load_market_data, load_snapshot
from strategies.factory import create_strategy
from utils.config import COMMISSION_MIN
from utils.versioning import get_code_version, get_environment_info, stable_hash


RECORD_VERSION = "experiment-v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXPERIMENT_DIR = os.environ.get("ABACKTEST_EXPERIMENT_DIR") or str(
    PROJECT_ROOT / "experiments"
)


@dataclass(frozen=True)
class CostAssumptions:
    """回测成本和交易规则假设。"""

    init_cash: float = 1_000_000.0
    commission: float = 0.00025
    commission_min: float = COMMISSION_MIN
    slippage: float = 0.001
    impact_coefficient: float = 0.02
    max_slippage: float = 0.05
    participation_rate: float = 0.05
    lot_size: int | None = 100
    stamp_tax: bool = True
    transfer_fee: bool = True
    t_plus_1: bool = True
    price_limit: bool = True
    limit_queue_fill_ratio: float = 0.25
    order_ttl_bars: int = 5
    execution_model: str = "realistic"
    rf: float = 0.0

    def __post_init__(self):
        for name in (
            "init_cash", "commission", "commission_min", "slippage",
            "impact_coefficient", "max_slippage", "participation_rate",
            "limit_queue_fill_ratio", "rf",
        ):
            object.__setattr__(self, name, float(getattr(self, name)))
        for name in ("stamp_tax", "transfer_fee", "t_plus_1", "price_limit"):
            object.__setattr__(self, name, bool(getattr(self, name)))

    def to_engine_kwargs(self, *, code: str | None = None) -> dict:
        values = asdict(self)
        values["trade_unit"] = values.pop("lot_size")
        values["code"] = code
        values.pop("rf", None)
        values["rf"] = self.rf
        return values

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CostAssumptions":
        allowed = set(cls.__dataclass_fields__)
        return cls(**{key: value for key, value in data.items() if key in allowed})


@dataclass
class ExperimentRecord:
    """一个完整实验的不可变清单。"""

    experiment_id: str
    name: str
    kind: str
    status: str
    created_at: str
    code_version: str
    environment: dict
    data: dict
    strategy: dict
    parameters: dict
    parameter_ranges: dict | None
    cost_assumptions: dict
    engine_config: dict
    random_seed: int | None
    signal: dict
    result: dict
    reproducibility_key: str
    result_hash: str
    warnings: list[str] = field(default_factory=list)
    artifacts: dict = field(default_factory=dict)
    record_version: str = RECORD_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ExperimentRecord":
        return cls(**data)


class ExperimentStore:
    """本地实验清单存储，实验 ID 每次运行唯一。"""

    def __init__(self, root=None):
        self.root = Path(root or DEFAULT_EXPERIMENT_DIR)

    def write(self, record: ExperimentRecord, *, results=None,
              signals=None, extra_artifacts=None) -> ExperimentRecord:
        run_dir = self.root / record.created_at[:10] / record.experiment_id
        run_dir.mkdir(parents=True, exist_ok=True)
        artifact_map = {
            "manifest": "manifest.json",
        }
        if results is not None:
            path = run_dir / "results.csv"
            _atomic_text(
                path,
                results.to_csv(index=False, lineterminator="\n"),
            )
            artifact_map["results"] = path.name
        if signals is not None:
            path = run_dir / "signals.csv"
            _atomic_text(
                path,
                signals.to_csv(index=False, lineterminator="\n"),
            )
            artifact_map["signals"] = path.name
        for name, frame in (extra_artifacts or {}).items():
            if frame is None:
                continue
            path = run_dir / f"{name}.csv"
            _atomic_text(
                path,
                frame.to_csv(index=True, lineterminator="\n"),
            )
            artifact_map[name] = path.name
        record.artifacts = artifact_map
        _atomic_text(
            run_dir / "manifest.json",
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
        )
        return record

    def load(self, experiment_id: str) -> ExperimentRecord:
        for path in self.root.rglob(f"{experiment_id}/manifest.json"):
            return ExperimentRecord.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
        raise FileNotFoundError(f"未找到实验记录：{experiment_id}")

    def list(self) -> list[ExperimentRecord]:
        records = []
        if not self.root.exists():
            return records
        for path in self.root.rglob("manifest.json"):
            try:
                records.append(ExperimentRecord.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                ))
            except Exception:
                continue
        return sorted(records, key=lambda item: item.created_at, reverse=True)

    def artifact_path(self, record: ExperimentRecord, name: str) -> Path:
        relative = record.artifacts[name]
        return self.root / record.created_at[:10] / record.experiment_id / relative


def run_backtest_experiment(df: pd.DataFrame, strategy_name: str,
                            params: dict | None = None, *,
                            code: str | None = None,
                            symbol: str | None = None,
                            start=None,
                            end=None,
                            frequency: str | None = None,
                            adjust: str | None = None,
                            costs: CostAssumptions | None = None,
                            name: str | None = None,
                            store: ExperimentStore | None = None,
                            record: bool = True) -> dict:
    """运行单次回测并保存完整实验记录。"""
    if not isinstance(strategy_name, str):
        strategy_name = strategy_name.strategy_key
    strategy = create_strategy(strategy_name, **(params or {}))
    signal = strategy.generate_signal_output(df)
    costs = costs or CostAssumptions()
    engine_kwargs = costs.to_engine_kwargs(code=code or symbol)
    engine = BacktestEngine(
        df,
        signal.entries,
        signal.exits,
        **engine_kwargs,
    ).run()
    analyzer = analyze_portfolio(engine.portfolio, rf=costs.rf)
    metrics = {
        "engine": engine.get_metrics(),
        "risk": analyzer.compute_metrics(),
    }
    result_hash = stable_hash(metrics)
    data_context = _data_context(
        df, symbol=symbol or code, start=start, end=end,
        frequency=frequency, adjust=adjust,
    )
    reproducibility_key = stable_hash({
        "kind": "backtest",
        "code_version": get_code_version(),
        "data": _identity_data_context(data_context),
        "strategy": strategy.metadata().metadata_hash,
        "parameters": strategy.params(),
        "cost_assumptions": costs.to_dict(),
        "engine_config": engine_kwargs,
    })
    experiment_id = _experiment_id("backtest", reproducibility_key)
    record_obj = ExperimentRecord(
        experiment_id=experiment_id,
        name=name or f"{strategy.name} 单次回测",
        kind="backtest",
        status="completed",
        created_at=datetime.now().isoformat(timespec="seconds"),
        code_version=get_code_version(),
        environment=get_environment_info(),
        data=data_context,
        strategy=strategy.metadata().to_dict(),
        parameters=strategy.params(),
        parameter_ranges=None,
        cost_assumptions=costs.to_dict(),
        engine_config=engine_kwargs,
        random_seed=None,
        signal=signal.to_dict(),
        result={
            "metrics": metrics,
            "result_hash": result_hash,
        },
        reproducibility_key=reproducibility_key,
        result_hash=result_hash,
        warnings=_record_warnings(data_context, signal.to_dict()),
    )
    if record:
        ExperimentStore(store.root if isinstance(store, ExperimentStore) else store) \
            .write(record_obj, signals=signal.to_frame())
    return {
        "experiment_id": experiment_id,
        "reproducibility_key": reproducibility_key,
        "result_hash": result_hash,
        "record": record_obj,
        "strategy": strategy,
        "signal": signal,
        "engine": engine,
        "metrics": metrics,
    }


def record_optimization_experiment(
        df: pd.DataFrame,
        *,
        strategy_name: str,
        parameter_ranges: dict,
        metric: str,
        method: str,
        output: dict,
        costs: CostAssumptions | None = None,
        code: str | None = None,
        symbol: str | None = None,
        start=None,
        end=None,
        frequency: str | None = None,
        adjust: str | None = None,
        random_seed: int | None = None,
        name: str | None = None,
        store: ExperimentStore | None = None,
) -> ExperimentRecord:
    """记录网格搜索或遗传算法实验。"""
    strategy = create_strategy(strategy_name, **output["best_params"])
    signal = strategy.generate_signal_output(df)
    costs = costs or CostAssumptions()
    results = output["results"]
    result_hash = _frame_hash(results)
    data_context = _data_context(
        df, symbol=symbol or code, start=start, end=end,
        frequency=frequency, adjust=adjust,
    )
    normalized_ranges = _json_ranges(parameter_ranges)
    reproducibility_key = stable_hash({
        "kind": "optimization",
        "method": method,
        "metric": metric,
        "code_version": get_code_version(),
        "data": _identity_data_context(data_context),
        "strategy": strategy.metadata().metadata_hash,
        "parameter_ranges": normalized_ranges,
        "cost_assumptions": costs.to_dict(),
        "random_seed": random_seed,
    })
    experiment_id = _experiment_id("optimization", reproducibility_key)
    record_obj = ExperimentRecord(
        experiment_id=experiment_id,
        name=name or f"{strategy.name} {method} 参数寻优",
        kind="optimization",
        status="completed",
        created_at=datetime.now().isoformat(timespec="seconds"),
        code_version=get_code_version(),
        environment=get_environment_info(),
        data=data_context,
        strategy=strategy.metadata().to_dict(),
        parameters=output["best_params"],
        parameter_ranges=normalized_ranges,
        cost_assumptions=costs.to_dict(),
        engine_config={
            **costs.to_engine_kwargs(code=code or symbol),
            "metric": metric,
            "method": method,
        },
        random_seed=random_seed,
        signal=signal.to_dict(),
        result={
            "best_params": output["best_params"],
            "best_value": output["best_value"],
            "best_metrics": output["best_metrics"],
            "result_hash": result_hash,
        },
        reproducibility_key=reproducibility_key,
        result_hash=result_hash,
        warnings=_record_warnings(data_context, signal.to_dict()),
    )
    ExperimentStore(
        store.root if isinstance(store, ExperimentStore) else store
    ).write(record_obj, results=results, signals=signal.to_frame())
    return record_obj


def record_batch_experiment(
        dfs: dict,
        *,
        codes: list[str],
        strategy_configs: list[dict],
        output: dict,
        costs: CostAssumptions | None = None,
        start=None,
        end=None,
        frequency: str = "daily",
        adjust: str | None = None,
        max_workers: int | None = None,
        name: str | None = None,
        store: ExperimentStore | None = None,
) -> ExperimentRecord:
    """记录多股票、多策略批量回测实验。"""
    costs = costs or CostAssumptions()
    strategy_contexts = _batch_strategy_contexts(strategy_configs)
    contexts = {
        code: _data_context(
            dfs[code],
            symbol=code,
            start=start,
            end=end,
            frequency=frequency,
            adjust=adjust,
        )
        for code in codes
        if code in dfs
    }
    results = output["results"]
    result_hash = _frame_hash(results)
    reproducibility_key = stable_hash({
        "kind": "batch_backtest",
        "code_version": get_code_version(),
        "requested_codes": list(codes),
        "data": {
            code: _identity_data_context(context)
            for code, context in sorted(contexts.items())
        },
        "strategy_configs": strategy_configs,
        "strategy_metadata": strategy_contexts,
        "cost_assumptions": costs.to_dict(),
        "frequency": frequency,
    })
    experiment_id = _experiment_id("batch", reproducibility_key)
    record_obj = ExperimentRecord(
        experiment_id=experiment_id,
        name=name or "多股票多策略批量回测",
        kind="batch_backtest",
        status="completed",
        created_at=datetime.now().isoformat(timespec="seconds"),
        code_version=get_code_version(),
        environment=get_environment_info(),
        data={
            "datasets": contexts,
            "requested_codes": list(codes),
            "frequency": frequency,
            "adjust": adjust,
            "start": str(start or ""),
            "end": str(end or ""),
        },
        strategy={
            "configs": strategy_configs,
            "contexts": strategy_contexts,
        },
        parameters={},
        parameter_ranges=None,
        cost_assumptions=costs.to_dict(),
        engine_config={
            **costs.to_engine_kwargs(),
            "max_workers": max_workers,
        },
        random_seed=None,
        signal={},
        result={
            "success_count": len(results),
            "failed_count": len(output.get("failed", [])),
            "elapsed": output.get("elapsed"),
            "result_hash": result_hash,
        },
        reproducibility_key=reproducibility_key,
        result_hash=result_hash,
        warnings=list(output.get("failed", [])),
    )
    ExperimentStore(
        store.root if isinstance(store, ExperimentStore) else store
    ).write(record_obj, results=results)
    return record_obj


def record_portfolio_experiment(
        data: dict,
        target_weights: pd.DataFrame,
        result,
        *,
        execution_config,
        constraints,
        rebalance_frequency,
        rebalance_threshold: float,
        rf: float = 0.0,
        name: str | None = None,
        store: ExperimentStore | None = None,
) -> ExperimentRecord:
    """记录多标的组合回测实验。"""
    contexts = {
        symbol: _data_context(
            frame,
            symbol=symbol,
            start=frame.index.min() if len(frame) else None,
            end=frame.index.max() if len(frame) else None,
            frequency=_frame_frequency(frame),
            adjust=frame.attrs.get("adjust"),
        )
        for symbol, frame in data.items()
    }
    result_frame = result.equity.to_frame("equity")
    result_hash = _frame_hash(result_frame)
    target_hash = _frame_hash(target_weights)
    constraints_dict = asdict(constraints)
    config_dict = execution_config.to_dict()
    reproducibility_key = stable_hash({
        "kind": "portfolio_backtest",
        "code_version": get_code_version(),
        "data": {
            symbol: _identity_data_context(context)
            for symbol, context in sorted(contexts.items())
        },
        "target_weights_hash": target_hash,
        "constraints": constraints_dict,
        "rebalance_frequency": str(rebalance_frequency),
        "rebalance_threshold": float(rebalance_threshold),
        "execution_config": config_dict,
        "rf": float(rf),
    })
    experiment_id = _experiment_id("portfolio", reproducibility_key)
    record_obj = ExperimentRecord(
        experiment_id=experiment_id,
        name=name or "多标的组合回测",
        kind="portfolio_backtest",
        status="completed",
        created_at=datetime.now().isoformat(timespec="seconds"),
        code_version=get_code_version(),
        environment=get_environment_info(),
        data={
            "datasets": contexts,
            "symbols": list(data),
            "target_weights_hash": target_hash,
        },
        strategy={
            "type": "target_weights",
            "constraints": constraints_dict,
        },
        parameters={
            "rebalance_frequency": str(rebalance_frequency),
            "rebalance_threshold": float(rebalance_threshold),
        },
        parameter_ranges=None,
        cost_assumptions=CostAssumptions(
            init_cash=execution_config.init_cash,
            commission=execution_config.commission,
            commission_min=execution_config.commission_min,
            slippage=execution_config.slippage,
            impact_coefficient=execution_config.impact_coefficient,
            max_slippage=execution_config.max_slippage,
            participation_rate=execution_config.participation_rate,
            lot_size=execution_config.lot_size,
            stamp_tax=execution_config.stamp_tax,
            transfer_fee=execution_config.transfer_fee,
            t_plus_1=execution_config.t_plus_1,
            price_limit=execution_config.price_limit,
            limit_queue_fill_ratio=execution_config.limit_queue_fill_ratio,
            order_ttl_bars=execution_config.order_ttl_bars,
            execution_model="realistic",
            rf=rf,
        ).to_dict(),
        engine_config=config_dict,
        random_seed=None,
        signal={
            "type": "target_weights",
            "rows": len(target_weights),
            "columns": list(target_weights.columns),
        },
        result={
            "metrics": result.metrics,
            "result_hash": result_hash,
        },
        reproducibility_key=reproducibility_key,
        result_hash=result_hash,
        warnings=[
            f"{symbol} 数据质量 {context['quality'].get('status')}"
            for symbol, context in contexts.items()
            if context.get("quality", {}).get("status") not in (None, "PASS")
        ],
    )
    ExperimentStore(
        store.root if isinstance(store, ExperimentStore) else store
    ).write(
        record_obj,
        results=result.pnl_contribution,
        extra_artifacts={
            "target_weights": target_weights,
            "actual_weights": result.weights,
            "positions": result.position_values,
        },
    )
    return record_obj


def reproduce_experiment(experiment_id: str, *, store=None,
                         data_loader=None) -> dict:
    """重放一个实验并比较输入指纹和结果哈希。"""
    experiment_store = store if isinstance(store, ExperimentStore) \
        else ExperimentStore(store)
    record = experiment_store.load(experiment_id)
    costs = CostAssumptions.from_dict(record.cost_assumptions)

    if record.kind == "backtest":
        df = _load_experiment_data(record, data_loader=data_loader)
        strategy_name = record.strategy["key"]
        actual = run_backtest_experiment(
            df,
            strategy_name,
            record.parameters,
            code=record.engine_config.get("code"),
            symbol=record.data.get("symbol"),
            start=record.data.get("start"),
            end=record.data.get("end"),
            frequency=record.data.get("frequency"),
            adjust=record.data.get("adjust"),
            costs=costs,
            name=record.name,
            store=experiment_store,
            record=False,
        )
    elif record.kind == "optimization":
        from optimization import optimize_parameters

        df = _load_experiment_data(record, data_loader=data_loader)
        strategy_name = record.strategy["key"]
        current_strategy = create_strategy(strategy_name, **record.parameters)
        method = record.engine_config["method"]
        if method == "core_grid":
            from core.optimizer import grid_search as core_grid_search

            result = core_grid_search(
                df,
                current_strategy.__class__,
                record.parameter_ranges,
                metric=record.engine_config["metric"],
                engine_kwargs=costs.to_engine_kwargs(
                    code=record.engine_config.get("code")
                ),
            )
            actual_output = {"results": result}
        else:
            optimization_kwargs = {
                "init_cash": costs.init_cash,
                "commission": costs.commission,
                "slippage": costs.slippage,
                "code": record.engine_config.get("code"),
                "rf": costs.rf,
                "t_plus_1": costs.t_plus_1,
                "price_limit": costs.price_limit,
                "stamp_tax": costs.stamp_tax,
                "transfer_fee": costs.transfer_fee,
                "commission_min": costs.commission_min,
                "participation_rate": costs.participation_rate,
                "impact_coefficient": costs.impact_coefficient,
                "max_slippage": costs.max_slippage,
                "limit_queue_fill_ratio": costs.limit_queue_fill_ratio,
                "order_ttl_bars": costs.order_ttl_bars,
                "execution_model": costs.execution_model,
                "trade_unit": costs.lot_size,
                "verbose": False,
                "record_experiment": False,
            }
            if method in ("genetic", "ga"):
                optimization_kwargs["seed"] = record.random_seed
            actual_output = optimize_parameters(
                df,
                strategy_name,
                record.parameter_ranges,
                metric=record.engine_config["metric"],
                method=method,
                **optimization_kwargs,
            )
        actual = {
            "reproducibility_key": stable_hash({
                "kind": "optimization",
                "method": method,
                "metric": record.engine_config["metric"],
                "code_version": get_code_version(),
                "data": _identity_data_context(
                    _data_context(
                        df,
                        symbol=record.data.get("symbol"),
                        start=record.data.get("start"),
                        end=record.data.get("end"),
                        frequency=record.data.get("frequency"),
                        adjust=record.data.get("adjust"),
                    )
                ),
                "strategy": current_strategy.metadata().metadata_hash,
                "parameter_ranges": record.parameter_ranges,
                "cost_assumptions": costs.to_dict(),
                "random_seed": record.random_seed,
            }),
            "result_hash": _frame_hash(actual_output["results"]),
            "output": actual_output,
        }
    elif record.kind == "batch_backtest":
        from core.batch_backtest import run_batch

        dfs = _load_batch_experiment_data(record, data_loader=data_loader)
        configs = record.strategy["configs"]
        strategy_contexts = _batch_strategy_contexts(configs)
        frequency = record.data.get("frequency", "daily")
        actual_output = run_batch(
            list(record.data["datasets"]),
            configs,
            init_cash=costs.init_cash,
            commission=costs.commission,
            commission_min=costs.commission_min,
            slippage=costs.slippage,
            start=record.data.get("start") or None,
            end=record.data.get("end") or None,
            rf=costs.rf,
            freq=frequency,
            t_plus_1=costs.t_plus_1,
            price_limit=costs.price_limit,
            stamp_tax=costs.stamp_tax,
            transfer_fee=costs.transfer_fee,
            participation_rate=costs.participation_rate,
            impact_coefficient=costs.impact_coefficient,
            max_slippage=costs.max_slippage,
            limit_queue_fill_ratio=costs.limit_queue_fill_ratio,
            order_ttl_bars=costs.order_ttl_bars,
            execution_model=costs.execution_model,
            trade_unit=costs.lot_size,
            max_workers=1,
            data_map=dfs,
            record_experiment=False,
        )
        contexts = {
            code: _data_context(
                dfs[code],
                symbol=code,
                start=record.data.get("start"),
                end=record.data.get("end"),
                frequency=frequency,
                adjust=record.data.get("adjust"),
            )
            for code in record.data["datasets"]
        }
        actual = {
            "reproducibility_key": stable_hash({
                "kind": "batch_backtest",
                "code_version": get_code_version(),
                "requested_codes": list(record.data.get("requested_codes", record.data["datasets"])),
                "data": {
                    code: _identity_data_context(context)
                    for code, context in sorted(contexts.items())
                },
                "strategy_configs": configs,
                "strategy_metadata": strategy_contexts,
                "cost_assumptions": costs.to_dict(),
                "frequency": frequency,
            }),
            "result_hash": _frame_hash(actual_output["results"]),
            "output": actual_output,
        }
    elif record.kind == "portfolio_backtest":
        from core.portfolio_backtest import (
            PortfolioBacktestEngine,
            PortfolioConstraints,
        )

        dfs = _load_batch_experiment_data(record, data_loader=data_loader)
        target_path = experiment_store.artifact_path(
            record, "target_weights"
        )
        target_weights = pd.read_csv(
            target_path, index_col=0, parse_dates=True
        )
        constraints = PortfolioConstraints(
            **record.strategy["constraints"]
        )
        actual_output = PortfolioBacktestEngine(
            dfs,
            target_weights,
            rebalance_frequency=record.parameters["rebalance_frequency"],
            rebalance_threshold=record.parameters["rebalance_threshold"],
            constraints=constraints,
            **_portfolio_engine_kwargs(costs),
        ).run(record_experiment=False)
        contexts = {
            symbol: _data_context(
                frame,
                symbol=symbol,
                start=frame.index.min(),
                end=frame.index.max(),
                frequency=_frame_frequency(frame),
                adjust=frame.attrs.get("adjust"),
            )
            for symbol, frame in dfs.items()
        }
        actual = {
            "reproducibility_key": stable_hash({
                "kind": "portfolio_backtest",
                "code_version": get_code_version(),
                "data": {
                    symbol: _identity_data_context(context)
                    for symbol, context in sorted(contexts.items())
                },
                "target_weights_hash": _frame_hash(target_weights),
                "constraints": asdict(constraints),
                "rebalance_frequency": record.parameters[
                    "rebalance_frequency"
                ],
                "rebalance_threshold": record.parameters[
                    "rebalance_threshold"
                ],
                "execution_config": actual_output.execution_config.to_dict(),
                "rf": costs.rf,
            }),
            "result_hash": _frame_hash(
                actual_output.result.equity.to_frame("equity")
            ),
            "output": actual_output,
        }
    else:
        raise ValueError(f"不支持的实验类型：{record.kind!r}")

    differences = []
    if actual["reproducibility_key"] != record.reproducibility_key:
        differences.append("reproducibility_key")
    if actual["result_hash"] != record.result_hash:
        differences.append("result_hash")
    return {
        "experiment_id": experiment_id,
        "matches": not differences,
        "differences": differences,
        "expected": {
            "reproducibility_key": record.reproducibility_key,
            "result_hash": record.result_hash,
        },
        "actual": {
            "reproducibility_key": actual["reproducibility_key"],
            "result_hash": actual["result_hash"],
        },
        "actual_output": actual,
    }


def _load_experiment_data(record: ExperimentRecord, *, data_loader=None):
    if data_loader is not None:
        return data_loader(record.data)
    snapshot_id = record.data.get("snapshot_id")
    if snapshot_id:
        return load_snapshot(snapshot_id, symbol=record.data.get("symbol"))
    symbol = record.data.get("symbol")
    start = record.data.get("start")
    end = record.data.get("end")
    if not symbol or not start or not end:
        raise ValueError("实验记录缺少复现所需的数据范围")
    return load_market_data(
        symbol,
        start,
        end,
        freq=record.data.get("frequency", "daily"),
        adjust=record.data.get("adjust", "qfq"),
        strict=True,
    )


def _load_batch_experiment_data(record: ExperimentRecord, *, data_loader=None):
    if data_loader is not None:
        loaded = data_loader(record.data)
        if isinstance(loaded, dict):
            return loaded
        return dict(loaded)
    frames = {}
    for code, context in record.data["datasets"].items():
        snapshot_id = context.get("snapshot_id")
        if snapshot_id:
            frames[code] = load_snapshot(snapshot_id, symbol=code)
        else:
            frames[code] = load_market_data(
                code,
                context["start"],
                context["end"],
                freq=context.get("frequency", "daily"),
                adjust=context.get("adjust", "qfq"),
                strict=True,
            )
    return frames


def _batch_strategy_contexts(strategy_configs: list[dict]) -> list[dict]:
    contexts = []
    for config in strategy_configs:
        strategy = create_strategy(config["name"], **config.get("params", {}))
        contexts.append({
            "config": config,
            "metadata": strategy.metadata().to_dict(),
            "parameters": strategy.params(),
        })
    return contexts


def _portfolio_engine_kwargs(costs: CostAssumptions) -> dict:
    return {
        "init_cash": costs.init_cash,
        "commission": costs.commission,
        "commission_min": costs.commission_min,
        "slippage": costs.slippage,
        "impact_coefficient": costs.impact_coefficient,
        "max_slippage": costs.max_slippage,
        "participation_rate": costs.participation_rate,
        "lot_size": costs.lot_size,
        "t_plus_1": costs.t_plus_1,
        "price_limit": costs.price_limit,
        "stamp_tax": costs.stamp_tax,
        "transfer_fee": costs.transfer_fee,
        "limit_queue_fill_ratio": costs.limit_queue_fill_ratio,
        "order_ttl_bars": costs.order_ttl_bars,
        "rf": costs.rf,
    }


def _frame_frequency(frame: pd.DataFrame) -> str:
    if frame is None or len(frame.index) < 2:
        return "daily"
    index = pd.DatetimeIndex(frame.index)
    median_seconds = float(
        (index[1:] - index[:-1]).median().total_seconds()
    )
    if median_seconds < 86400:
        return f"{max(1, int(round(median_seconds / 60)))}min"
    return "daily"


def _data_context(df: pd.DataFrame, *, symbol, start, end, frequency, adjust) -> dict:
    manifest = dict(df.attrs.get("snapshot_manifest", {}))
    frequency = Frequency.parse(
        frequency or manifest.get("frequency") or "daily"
    ).value
    return {
        "symbol": symbol or manifest.get("symbol"),
        "frequency": frequency,
        "adjust": adjust or manifest.get("adjust") or "qfq",
        "start": str(start or manifest.get("actual_start") or ""),
        "end": str(end or manifest.get("actual_end") or ""),
        "data_version": df.attrs.get("data_version", "unknown"),
        "snapshot_id": manifest.get("snapshot_id"),
        "schema_version": df.attrs.get("schema_version", "unknown"),
        "provider": df.attrs.get("provider", manifest.get("provider", "unknown")),
        "quality": df.attrs.get("data_quality", {}),
    }


def _record_warnings(data_context: dict, signal: dict) -> list[str]:
    warnings = []
    if data_context.get("data_version") in (None, "", "unknown"):
        warnings.append("行情数据没有可追踪的 data_version")
    quality = data_context.get("quality") or {}
    if quality.get("status") not in (None, "PASS"):
        warnings.append(f"行情质量状态为 {quality.get('status')}")
    if signal.get("entry_count", 0) == 0:
        warnings.append("策略没有产生买入信号")
    return warnings


def _identity_data_context(data_context: dict) -> dict:
    quality = data_context.get("quality") or {}
    return {
        "symbol": data_context.get("symbol"),
        "frequency": data_context.get("frequency"),
        "adjust": data_context.get("adjust"),
        "start": data_context.get("start"),
        "end": data_context.get("end"),
        "data_version": data_context.get("data_version"),
        "snapshot_id": data_context.get("snapshot_id"),
        "schema_version": data_context.get("schema_version"),
        "provider": data_context.get("provider"),
        "quality_status": quality.get("status"),
        "quality_score": quality.get("score"),
        "calendar_version": quality.get("calendar_version"),
        "issue_codes": [
            item.get("code") for item in quality.get("issues", [])
        ],
    }


def _experiment_id(kind: str, reproducibility_key: str) -> str:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    return f"{stamp}_{kind}_{reproducibility_key[:8]}"


def _frame_hash(frame: pd.DataFrame) -> str:
    payload = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _json_ranges(ranges: dict) -> dict:
    result = {}
    for key, value in ranges.items():
        if isinstance(value, range):
            result[key] = list(value)
        elif isinstance(value, tuple):
            result[key] = list(value)
        elif isinstance(value, dict):
            result[key] = dict(value)
        else:
            result[key] = value
    return result


def _atomic_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
