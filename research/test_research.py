"""策略元数据、信号和实验复现的离线测试。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from optimization import optimize_parameters
from core.optimizer import optimize as legacy_optimize
from core.batch_backtest import run_batch
from strategies.double_ma import DoubleMAStrategy
from research.experiments import (
    ExperimentStore,
    reproduce_experiment,
    run_backtest_experiment,
)
from strategies.factory import create_strategy, get_available_strategies


def _frame():
    index = pd.date_range("2024-01-01", periods=180, freq="D")
    close = pd.Series(
        np.r_[
            np.linspace(10, 18, 60),
            np.linspace(18, 12, 60),
            np.linspace(12, 20, 60),
        ],
        index=index,
    )
    frame = pd.DataFrame({
        "date": index,
        "open": close,
        "high": close + 0.3,
        "low": close - 0.3,
        "close": close,
        "volume": 100_000,
    })
    frame.attrs["data_version"] = "test-data-v1"
    frame.attrs["schema_version"] = "market-data-v1"
    frame.attrs["provider"] = "test"
    frame.attrs["adjust"] = "qfq"
    frame.attrs["data_quality"] = {
        "status": "PASS",
        "score": 100,
        "calendar_version": "test-calendar-v1",
        "issues": [],
    }
    return frame


def test_strategy_metadata_and_parameter_validation():
    strategies = get_available_strategies()
    assert len(strategies) == 5
    keys = [item["strategy_key"] for item in strategies]
    assert len(keys) == len(set(keys))
    for item in strategies:
        strategy = create_strategy(item["key"])
        metadata = strategy.metadata()
        assert metadata.key == item["strategy_key"]
        assert metadata.version == item["version"]
        assert metadata.metadata_hash
        assert set(strategy.params()) == set(metadata.default_params)
    try:
        create_strategy("双均线", unknown=1)
    except ValueError:
        pass
    else:
        raise AssertionError("未知参数应被拒绝")
    print("PASS strategy metadata")


def test_standard_signal_output_is_stable():
    frame = _frame()
    first = create_strategy("双均线", fast=3, slow=10) \
        .generate_signal_output(frame)
    second = create_strategy("双均线", fast=3, slow=10) \
        .generate_signal_output(frame)
    assert first.signal_hash == second.signal_hash
    output = first.to_frame()
    assert list(output.columns) == ["date", "signal", "entry", "exit"]
    assert set(output["signal"].unique()) <= {-1, 0, 1}
    assert not (output["entry"] & output["exit"]).any()
    print("PASS standard signal output")


def test_backtest_experiment_is_recorded_and_reproducible():
    frame = _frame()
    with tempfile.TemporaryDirectory() as tmp:
        store = ExperimentStore(tmp)
        first = run_backtest_experiment(
            frame,
            "双均线",
            {"fast": 3, "slow": 10},
            code="600519",
            symbol="600519",
            store=store,
            name="metadata test",
        )
        second = run_backtest_experiment(
            frame,
            "双均线",
            {"fast": 3, "slow": 10},
            code="600519",
            symbol="600519",
            store=store,
        )
        assert first["reproducibility_key"] == second["reproducibility_key"]
        assert first["result_hash"] == second["result_hash"]
        assert first["experiment_id"] != second["experiment_id"]
        record = store.load(first["experiment_id"])
        assert record.data["data_version"] == "test-data-v1"
        assert record.strategy["key"] == "double_ma"
        assert store.artifact_path(record, "signals").exists()
        reproduced = reproduce_experiment(
            first["experiment_id"],
            store=store,
            data_loader=lambda context: frame.copy(),
        )
        assert reproduced["matches"], reproduced["differences"]
    print("PASS backtest experiment")


def test_optimization_experiment_is_recorded_and_reproducible():
    frame = _frame()
    with tempfile.TemporaryDirectory() as tmp:
        store = ExperimentStore(tmp)
        output = optimize_parameters(
            frame,
            "双均线",
            {"fast": [3, 5], "slow": [8, 10]},
            metric="累计收益率",
            method="grid",
            code="600519",
            max_workers=1,
            verbose=False,
            experiment_store=store,
        )
        assert output["experiment_id"]
        record = store.load(output["experiment_id"])
        assert record.kind == "optimization"
        assert record.result["best_params"]
        assert store.artifact_path(record, "results").exists()
        reproduced = reproduce_experiment(
            output["experiment_id"],
            store=store,
            data_loader=lambda context: frame.copy(),
        )
        assert reproduced["matches"], reproduced["differences"]
    print("PASS optimization experiment")


def test_legacy_optimizer_is_reproducible():
    frame = _frame()
    with tempfile.TemporaryDirectory() as tmp:
        store = ExperimentStore(tmp)
        output = legacy_optimize(
            frame,
            DoubleMAStrategy,
            {"fast": [3, 5], "slow": [8, 10]},
            metric="累计收益率",
            engine_kwargs={"code": "600519"},
            experiment_store=store,
        )
        assert output["experiment_id"]
        record = store.load(output["experiment_id"])
        assert record.engine_config["method"] == "core_grid"
        reproduced = reproduce_experiment(
            output["experiment_id"],
            store=store,
            data_loader=lambda context: frame.copy(),
        )
        assert reproduced["matches"], reproduced["differences"]
    print("PASS legacy optimizer experiment")


def test_batch_experiment_is_recorded_and_reproducible():
    frame = _frame()
    data_map = {"600519": frame.copy(), "000001": frame.copy()}
    configs = [{"name": "双均线", "params": {"fast": 3, "slow": 10}}]
    with tempfile.TemporaryDirectory() as tmp:
        store = ExperimentStore(tmp)
        output = run_batch(
            list(data_map),
            configs,
            start="2024-01-01",
            end="2024-06-30",
            max_workers=1,
            data_map=data_map,
            experiment_store=store,
        )
        assert output["experiment_id"]
        record = store.load(output["experiment_id"])
        assert record.kind == "batch_backtest"
        assert len(record.data["datasets"]) == 2
        assert store.artifact_path(record, "results").exists()
        reproduced = reproduce_experiment(
            output["experiment_id"],
            store=store,
            data_loader=lambda context: data_map,
        )
        assert reproduced["matches"], reproduced["differences"]
    print("PASS batch experiment")


def run_test():
    for test in (
        test_strategy_metadata_and_parameter_validation,
        test_standard_signal_output_is_stable,
        test_backtest_experiment_is_recorded_and_reproducible,
        test_optimization_experiment_is_recorded_and_reproducible,
        test_legacy_optimizer_is_reproducible,
        test_batch_experiment_is_recorded_and_reproducible,
    ):
        test()
    print("===== research tests passed =====")


if __name__ == "__main__":
    run_test()
