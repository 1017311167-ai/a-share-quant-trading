"""策略稳健性和过拟合风险离线测试。"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research.validation import (
    ValidationConfig,
    compare_strategy_robustness,
    evaluate_strategy_robustness,
    monte_carlo_validation,
)
from research.experiments import ExperimentStore, reproduce_experiment


def _frame():
    index = pd.date_range("2022-01-01", periods=420, freq="D")
    trend = np.linspace(10, 18, len(index))
    cycle = 1.5 * np.sin(np.linspace(0, 16 * np.pi, len(index)))
    noise = np.random.default_rng(7).normal(0, 0.08, len(index))
    close = pd.Series(trend + cycle + noise, index=index)
    frame = pd.DataFrame({
        "date": index,
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": close + 0.2,
        "low": close - 0.2,
        "close": close,
        "volume": 2_000_000,
    })
    frame.attrs["data_version"] = "validation-test-v1"
    return frame


def test_monte_carlo_distribution():
    returns = pd.Series(
        np.random.default_rng(1).normal(0.0005, 0.01, 200)
    )
    result = monte_carlo_validation(returns, runs=100, seed=7)
    assert result["runs"] == 100
    assert 0 <= result["probability_positive"] <= 1
    assert set(result["terminal_return"]) == {
        "p05", "p25", "median", "p75", "p95"
    }
    print("PASS monte carlo")


def test_strategy_robustness_report():
    frame = _frame()
    config = ValidationConfig(
        in_sample_ratio=0.7,
        n_splits=3,
        train_ratio=0.5,
        window_mode="anchored",
        monte_carlo_runs=100,
        monte_carlo_seed=11,
        top_k=4,
        min_train_bars=60,
        min_test_bars=20,
    )
    report = evaluate_strategy_robustness(
        frame,
        "双均线",
        {"fast": [3, 5], "slow": [12, 20]},
        metric="夏普比率",
        method="grid",
        config=config,
        optimizer_kwargs={"max_workers": 1, "code": "600519"},
        record_experiment=False,
    )
    assert report.in_sample
    assert report.out_sample
    assert report.walk_forward["fold_count"] >= 1
    assert 0 <= report.parameter_stability["stability_score"] <= 1
    assert 0 <= report.monte_carlo["probability_positive"] <= 1
    assert report.overfitting["risk_level"] in {"LOW", "MEDIUM", "HIGH"}
    assert "excess_return" in report.benchmark
    json.dumps(report.to_dict())
    row = report.summary_row()
    assert row["策略"] == "双均线策略"
    assert "过拟合风险" in row
    print("PASS strategy robustness report")


def test_multiple_strategy_comparison():
    frame = _frame()
    config = ValidationConfig(
        in_sample_ratio=0.7,
        n_splits=2,
        train_ratio=0.5,
        monte_carlo_runs=80,
        top_k=3,
        min_train_bars=60,
        min_test_bars=20,
    )
    table = compare_strategy_robustness(
        frame,
        [
            {
                "name": "双均线",
                "param_ranges": {"fast": [3, 5], "slow": [12, 20]},
            },
            {
                "name": "RSI超买超卖",
                "param_ranges": {
                    "period": [7, 14],
                    "oversold": [25.0, 30.0],
                    "overbought": [70.0, 75.0],
                },
            },
        ],
        config=config,
        optimizer_kwargs={"max_workers": 1},
    )
    assert len(table) == 2
    assert {"策略", "过拟合风险", "稳健性分数", "主要风险"} <= set(table.columns)
    assert set(table["过拟合风险"]) <= {"LOW", "MEDIUM", "HIGH", "评估失败"}
    print("PASS multiple strategy comparison")


def test_validation_experiment_is_reproducible():
    frame = _frame()
    config = ValidationConfig(
        in_sample_ratio=0.7,
        n_splits=2,
        train_ratio=0.5,
        monte_carlo_runs=80,
        top_k=3,
        min_train_bars=60,
        min_test_bars=20,
    )
    with tempfile.TemporaryDirectory() as tmp:
        store = ExperimentStore(tmp)
        report = evaluate_strategy_robustness(
            frame,
            "双均线",
            {"fast": [3, 5], "slow": [12, 20]},
            config=config,
            optimizer_kwargs={"max_workers": 1, "code": "600519"},
            experiment_store=store,
        )
        assert report.experiment_id
        record = store.load(report.experiment_id)
        assert record.kind == "validation"
        reproduced = reproduce_experiment(
            report.experiment_id,
            store=store,
            data_loader=lambda context: frame,
        )
        assert reproduced["matches"], reproduced["differences"]
    print("PASS validation experiment")


def run_test():
    for test in (
        test_monte_carlo_distribution,
        test_strategy_robustness_report,
        test_multiple_strategy_comparison,
        test_validation_experiment_is_reproducible,
    ):
        test()
    print("===== validation tests passed =====")


if __name__ == "__main__":
    run_test()
