"""研究工作台总览：回测、样本外、参数稳定性、配置和数据质量。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

from app.trading_desk import (
    discover_database,
    environment_snapshot,
    render_environment_band,
)
from data import list_snapshots
from research import ExperimentStore


def render_research_overview():
    environment = environment_snapshot()
    render_environment_band(environment)
    st.subheader("研究总览")
    st.caption(
        f"{environment['kind']} · 策略研究、验证和运行配置均保留版本指纹"
    )
    records = _safe_records()
    backtests = [item for item in records if item.kind == "backtest"]
    validations = [item for item in records if item.kind == "validation"]
    optimizations = [item for item in records if item.kind == "optimization"]
    cols = st.columns(5)
    cols[0].metric("实验总数", len(records))
    cols[1].metric("普通回测", len(backtests))
    cols[2].metric("稳健性验证", len(validations))
    cols[3].metric("参数寻优", len(optimizations))
    cols[4].metric("数据快照", len(_safe_snapshots()))

    st.markdown("#### 最近回测")
    _render_backtest_summary(backtests[:10])
    st.markdown("#### 样本外与参数稳定性")
    _render_validation_summary(validations[:10])
    st.markdown("#### 运行配置")
    _render_runtime_config()
    st.markdown("#### 数据质量")
    _render_data_quality()


def _render_backtest_summary(records):
    rows = []
    for item in records:
        metrics = (
            (item.result or {}).get("metrics", {}).get("engine", {})
            if item.kind == "backtest" else {}
        )
        rows.append({
            "实验 ID": item.experiment_id,
            "名称": item.name,
            "创建时间": item.created_at,
            "累计收益率": metrics.get("累计收益率"),
            "最大回撤": metrics.get("最大回撤"),
            "夏普比率": metrics.get("夏普比率"),
            "数据版本": (item.data or {}).get("data_version", ""),
            "复现指纹": item.reproducibility_key,
        })
    if not rows:
        st.info("还没有回测实验。")
        return
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def _render_validation_summary(records):
    rows = []
    for item in records:
        summary = (item.result or {}).get("summary", {})
        rows.append({
            "实验 ID": item.experiment_id,
            "策略": (item.strategy or {}).get("name", ""),
            "样本内指标": summary.get("样本内指标"),
            "样本外指标": summary.get("样本外指标"),
            "参数稳定性": summary.get("参数稳定性"),
            "过拟合风险": summary.get("过拟合风险"),
            "稳健性分数": summary.get("稳健性分数"),
            "创建时间": item.created_at,
        })
    if not rows:
        st.info("还没有样本外或参数稳定性验证记录。")
        return
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def _render_runtime_config():
    environment = environment_snapshot()
    rows = [{
        "配置项": "TRADING_STAGE",
        "当前值": str(environment["stage"]),
        "说明": "当前工作阶段",
    }, {
        "配置项": "QMT_TRADING_MODE",
        "当前值": str(environment["qmt_mode"]),
        "说明": "券商环境声明",
    }, {
        "配置项": "QMT_ALLOW_REAL_TRADING",
        "当前值": str(environment["allow_real_trading"]),
        "说明": "真实资金开关，当前应为 false",
    }]
    path = discover_database()
    if path is not None:
        for item in _active_configs(path):
            rows.append({
                "配置项": item.get("config_name"),
                "当前值": str(item.get("version", "")),
                "说明": json.dumps(
                    item.get("payload", {}), ensure_ascii=False
                ),
            })
        rows.append({
            "配置项": "交易数据库",
            "当前值": str(path),
            "说明": "交易台读取的运行数据源",
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def _render_data_quality():
    snapshots = _safe_snapshots()
    if not snapshots:
        st.info("还没有行情数据快照。")
        return
    rows = []
    for item in snapshots[:50]:
        quality = item.quality or {}
        rows.append({
            "快照 ID": item.snapshot_id,
            "股票代码": item.symbol,
            "频率": item.frequency,
            "复权": item.adjust,
            "数据源": item.provider,
            "行数": item.rows,
            "数据范围": f"{item.actual_start} ~ {item.actual_end}",
            "质量状态": quality.get("status", ""),
            "质量分": quality.get("score", ""),
            "创建时间": item.created_at,
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def _safe_records():
    try:
        return ExperimentStore().list()
    except Exception:
        return []


def _safe_snapshots():
    try:
        return list_snapshots()
    except Exception:
        return []


def _active_configs(path):
    path = Path(path)
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("""
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='config_versions'
        """).fetchone()
        if row is None:
            return []
        rows = conn.execute("""
            SELECT * FROM config_versions
            WHERE active = 1 ORDER BY config_name
        """).fetchall()
        result = []
        for item in rows:
            value = dict(item)
            try:
                value["payload"] = json.loads(
                    value.pop("payload_json") or "{}"
                )
            except (TypeError, ValueError):
                value["payload"] = {}
            result.append(value)
        return result
    finally:
        conn.close()
