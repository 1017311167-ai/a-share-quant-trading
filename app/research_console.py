"""量化研究控制台。

整合数据质量、单股回测、组合回测、成本敏感性、稳健性验证和实验台账。
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.cost_analysis import (
    cost_sensitivity_from_strategy,
    summarize_cost_sensitivity,
)
from core.portfolio_backtest import (
    PortfolioBacktestEngine,
    PortfolioConstraints,
    signals_to_target_weights,
)
from data import (
    DataRequest,
    get_default_service,
    get_quality_report,
    list_snapshots,
    load_market_data,
)
from research import (
    CostAssumptions,
    ExperimentStore,
    ValidationConfig,
    evaluate_strategy_robustness,
    run_backtest_experiment,
)
from strategies.factory import get_available_strategies
from utils.versioning import get_code_version
from app.research_overview import render_research_overview
from app.trading_desk import (
    discover_database,
    environment_snapshot,
    render_trading_desk,
)


PLOT_CONFIG = {"displaylogo": False, "responsive": True}
FREQ_OPTIONS = {
    "日线": "daily",
    "1 分钟": "1min",
    "5 分钟": "5min",
    "15 分钟": "15min",
    "30 分钟": "30min",
    "60 分钟": "60min",
}


def main():
    st.set_page_config(
        page_title="A股量化交易与研究平台",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _apply_style()
    workspace = _workspace_sidebar()
    if workspace == "交易台":
        render_trading_desk()
        return
    page = st.session_state.get("console_page", "研究总览")
    if page == "研究总览":
        render_research_overview()
        return
    if page == "数据质量":
        page_data_quality()
    elif page == "单股回测":
        page_single_backtest()
    elif page == "组合回测":
        page_portfolio_backtest()
    elif page == "成本敏感性":
        page_cost_sensitivity()
    elif page == "稳健性验证":
        page_validation()
    else:
        page_experiments()


def _workspace_sidebar():
    environment = environment_snapshot()
    database = discover_database()
    with st.sidebar:
        st.markdown("## A股量化系统")
        color = "#177245" if environment["tone"] == "paper" else (
            "#b42318" if environment["tone"] == "danger" else "#52514e"
        )
        st.markdown(
            f"<div style='color:{color};font-weight:700;'>"
            f"{environment['kind']} · "
            f"{'禁止真实资金' if not environment['allow_real_trading'] else '真实资金开关开启'}"
            f"</div>",
            unsafe_allow_html=True,
        )
        st.caption(
            f"TRADING_STAGE={environment['stage']} · "
            f"QMT={environment['qmt_mode']}"
        )
        workspace_default = st.session_state.get(
            "workspace", "交易台"
        )
        workspace = st.radio(
            "工作区",
            ["交易台", "研究工作台"],
            index=0 if workspace_default == "交易台" else 1,
            key="workspace_selector",
        )
        st.session_state["workspace"] = workspace
        if workspace == "研究工作台":
            st.divider()
            pages = [
                "研究总览",
                "数据质量",
                "单股回测",
                "组合回测",
                "成本敏感性",
                "稳健性验证",
                "实验台账",
            ]
            default = st.session_state.get("console_page", "研究总览")
            page = st.radio(
                "研究功能",
                pages,
                index=pages.index(default) if default in pages else 0,
                label_visibility="collapsed",
            )
            st.session_state["console_page"] = page
        st.divider()
        try:
            experiment_count = len(ExperimentStore().list())
        except Exception:
            experiment_count = 0
        try:
            snapshot_count = len(list_snapshots())
        except Exception:
            snapshot_count = 0
        st.caption(f"交易数据库：{database or '未连接'}")
        st.caption(f"代码版本 {get_code_version()}")
        st.caption(f"实验记录 {experiment_count} · 数据快照 {snapshot_count}")
        return workspace


def page_data_quality():
    st.header("数据质量与快照")
    st.caption("统一检查行情完整性、异常值、停牌候选和数据版本。")
    with st.form("data_quality_form"):
        c1, c2, c3, c4 = st.columns([1.2, 1, 1, 1])
        code = c1.text_input("股票代码", value="600519")
        start, end = _date_range(c2, c3)
        freq_label = c4.selectbox("频率", list(FREQ_OPTIONS), index=0)
        adjust = c4.selectbox(
            "复权", ["qfq", "hfq", "none"], index=0,
            format_func={"qfq": "前复权", "hfq": "后复权", "none": "不复权"}.get,
        )
        submitted = st.form_submit_button("检查数据", type="primary", width="stretch")
    if not submitted:
        _show_snapshot_table(code=None)
        return
    try:
        request = DataRequest(
            code,
            start,
            end,
            FREQ_OPTIONS[freq_label],
            adjust,
            strict=False,
        )
        frame = load_market_data(
            code, start, end, freq=FREQ_OPTIONS[freq_label],
            adjust=adjust, strict=False,
        )
        report = get_default_service().check_quality(request, frame)
        quality = report.to_dict()
    except Exception as exc:
        st.error(f"数据检查失败：{exc}")
        return

    cols = st.columns(6)
    cols[0].metric("数据行数", f"{len(frame):,}")
    cols[1].metric("质量状态", quality["status"])
    cols[2].metric("质量分数", quality["score"])
    cols[3].metric("数据版本", frame.attrs.get("data_version", "unknown"))
    cols[4].metric("数据源", frame.attrs.get("provider", "unknown"))
    cols[5].metric("日历版本", quality.get("calendar_version", "unknown"))
    if quality["status"] == "FAIL":
        st.error("数据存在阻止正式流程使用的质量错误。")
    elif quality["status"] == "WARN":
        st.warning("数据可用于研究，但存在需要确认的警告。")
    else:
        st.success("数据质量检查通过。")

    st.plotly_chart(
        _price_figure(frame, f"{code} 行情"),
        width="stretch",
        config=PLOT_CONFIG,
    )
    tabs = st.tabs(["质量问题", "疑似停牌日期", "快照清单"])
    with tabs[0]:
        issues = pd.DataFrame(quality["issues"])
        if issues.empty:
            st.info("没有质量问题。")
        else:
            st.dataframe(issues, width="stretch", hide_index=True)
    with tabs[1]:
        dates = quality.get("suspension_candidates", [])
        if dates:
            st.dataframe(
                pd.DataFrame({"日期": dates}),
                width="stretch",
                hide_index=True,
            )
        else:
            st.info("没有发现疑似停牌或缺失交易日。")
    with tabs[2]:
        _show_snapshot_table(code=code)


def page_single_backtest():
    st.header("单股执行回测")
    st.caption("使用现实成交模型：次日开盘、最低佣金、参与率、部分成交和排队。")
    strategies = get_available_strategies()
    with st.sidebar:
        st.divider()
        st.caption("本次回测")
        code = st.text_input("股票代码", value="600519", key="single_code")
        start, end = _date_range(st, st, key_prefix="single")
        freq_label = st.selectbox("数据频率", list(FREQ_OPTIONS), key="single_freq")
        strategy_name = st.selectbox(
            "策略",
            [item["name"] for item in strategies],
            key="single_strategy",
        )
        strategy_info = next(
            item for item in strategies if item["name"] == strategy_name
        )
        params = _strategy_param_widgets(strategy_info, "single")
        with st.expander("交易成本和成交参数", expanded=False):
            init_cash = st.number_input(
                "初始资金", min_value=10_000.0, value=1_000_000.0,
                step=100_000.0,
            )
            commission = st.number_input(
                "佣金费率", min_value=0.0, value=0.00025,
                step=0.00005, format="%.5f",
            )
            commission_min = st.number_input(
                "最低佣金", min_value=0.0, value=5.0, step=1.0,
            )
            slippage = st.number_input(
                "基础滑点", min_value=0.0, value=0.001,
                step=0.0005, format="%.4f",
            )
            impact = st.number_input(
                "冲击成本系数", min_value=0.0, value=0.02, step=0.01,
            )
            participation = st.slider(
                "成交量参与率", min_value=0.01, max_value=1.0,
                value=0.05, step=0.01,
            )
        run = st.button("运行单股回测", type="primary", width="stretch")
    if run:
        try:
            with st.spinner("加载行情并运行成交模型..."):
                frame = load_market_data(code, start, end, freq=FREQ_OPTIONS[freq_label])
                result = run_backtest_experiment(
                    frame,
                    strategy_info["strategy_key"],
                    params,
                    code=code,
                    symbol=code,
                    frequency=FREQ_OPTIONS[freq_label],
                    costs=CostAssumptions(
                        init_cash=init_cash,
                        commission=commission,
                        commission_min=commission_min,
                        slippage=slippage,
                        impact_coefficient=impact,
                        participation_rate=participation,
                        rf=0.0,
                    ),
                    name=f"{code} {strategy_name} 单股回测",
                )
                st.session_state["single_result"] = result
        except Exception as exc:
            st.error(f"回测失败：{exc}")
    result = st.session_state.get("single_result")
    if not result:
        st.info("在左侧设置参数并运行回测。")
        return
    engine = result["engine"]
    metrics = engine.get_metrics()
    cols = st.columns(6)
    cols[0].metric("累计收益率", _pct(metrics["累计收益率"]))
    cols[1].metric("年化收益率", _pct(metrics["年化收益率"]))
    cols[2].metric("最大回撤", _pct(metrics["最大回撤"]))
    cols[3].metric("夏普比率", _num(metrics["夏普比率"]))
    cols[4].metric("成交率", _pct(metrics["成交率"]))
    cols[5].metric("平均滑点", _pct(metrics["平均滑点"]))
    st.caption(
        f"实验 ID：{result['experiment_id']} · "
        f"数据版本：{result['record'].data['data_version']} · "
        f"策略实现：{result['strategy'].metadata().implementation_hash}"
    )
    left, right = st.columns([2, 1])
    left.plotly_chart(
        _equity_figure(engine.get_equity()),
        width="stretch",
        config=PLOT_CONFIG,
    )
    right.plotly_chart(
        _drawdown_figure(engine.get_equity()),
        width="stretch",
        config=PLOT_CONFIG,
    )
    tabs = st.tabs(["绩效指标", "逐笔成交", "订单状态", "执行统计"])
    with tabs[0]:
        st.dataframe(
            pd.DataFrame(list(metrics.items()), columns=["指标", "数值"]),
            width="stretch",
            hide_index=True,
        )
    with tabs[1]:
        st.dataframe(engine.get_fills(), width="stretch", hide_index=True)
    with tabs[2]:
        st.dataframe(engine.get_orders(), width="stretch", hide_index=True)
    with tabs[3]:
        st.json(engine.get_execution_stats())


def page_portfolio_backtest():
    st.header("多标的组合回测")
    st.caption("共享资金池、目标权重、再平衡和组合级风险。")
    strategies = get_available_strategies()
    with st.sidebar:
        st.divider()
        st.caption("组合设置")
        codes_text = st.text_area(
            "股票代码",
            value="600519\n000001\n300750",
            height=100,
            key="portfolio_codes",
        )
        start, end = _date_range(st, st, key_prefix="portfolio")
        strategy_name = st.selectbox(
            "统一策略",
            [item["name"] for item in strategies],
            key="portfolio_strategy",
        )
        strategy_info = next(
            item for item in strategies if item["name"] == strategy_name
        )
        params = _strategy_param_widgets(strategy_info, "portfolio")
        rebalance = st.selectbox(
            "再平衡频率",
            ["daily", "weekly", "monthly", "quarterly"],
            index=2,
            format_func={
                "daily": "每日",
                "weekly": "每周",
                "monthly": "每月",
                "quarterly": "每季度",
            }.get,
        )
        max_weight = st.slider("单票最大权重", 0.05, 1.0, 0.35, 0.05)
        min_cash = st.slider("最低现金比例", 0.0, 0.5, 0.05, 0.01)
        max_positions = st.number_input(
            "最大持仓数", min_value=1, max_value=20, value=5, step=1,
        )
        run = st.button("运行组合回测", type="primary", width="stretch")
    if run:
        codes = [item.strip() for item in codes_text.splitlines() if item.strip()]
        try:
            with st.spinner("加载多标的行情并执行组合撮合..."):
                data = {
                    item: load_market_data(item, start, end, freq="daily")
                    for item in codes
                }
                strategy = strategy_info["strategy_key"]
                signals = {
                    item: _create_signal(strategy, params, frame)
                    for item, frame in data.items()
                }
                constraints = PortfolioConstraints(
                    max_weight=max_weight,
                    min_cash_weight=min_cash,
                    max_gross_exposure=1.0 - min_cash,
                    max_positions=int(max_positions),
                )
                targets = signals_to_target_weights(
                    signals,
                    next(iter(data.values())).index,
                    list(data),
                    constraints=constraints,
                )
                engine = PortfolioBacktestEngine(
                    data,
                    targets,
                    rebalance_frequency=rebalance,
                    constraints=constraints,
                ).run(experiment_name=f"{strategy_name} 等权组合")
                st.session_state["portfolio_result"] = engine
        except Exception as exc:
            st.error(f"组合回测失败：{exc}")
    engine = st.session_state.get("portfolio_result")
    if engine is None:
        st.info("在左侧设置标的、策略和约束后运行组合回测。")
        return
    metrics = engine.get_metrics()
    cols = st.columns(6)
    cols[0].metric("组合收益", _pct(metrics["累计收益率"]))
    cols[1].metric("最大回撤", _pct(metrics["最大回撤"]))
    cols[2].metric("最大总仓位", _pct(metrics["最大总仓位"]))
    cols[3].metric("HHI", _num(metrics["平均持仓集中度HHI"]))
    cols[4].metric("日 VaR 95%", _pct(metrics["日VaR95"]))
    cols[5].metric("换手率", _num(metrics["累计换手率"]))
    st.caption(f"实验 ID：{engine.experiment_id}")
    left, right = st.columns([2, 1])
    left.plotly_chart(
        _equity_figure(engine.get_equity()),
        width="stretch",
        config=PLOT_CONFIG,
    )
    latest_weights = engine.get_weights().iloc[-1].sort_values(ascending=False)
    right.plotly_chart(
        go.Figure(go.Bar(
            x=latest_weights.index,
            y=latest_weights.values,
            marker_color="#2a78d6",
        )).update_layout(
            title="最新实际权重",
            yaxis_tickformat=".0%",
            margin=dict(l=20, r=20, t=50, b=20),
        ),
        width="stretch",
        config=PLOT_CONFIG,
    )
    tabs = st.tabs(["持仓和暴露", "收益贡献", "成交", "订单", "相关性"])
    with tabs[0]:
        st.dataframe(engine.get_weights().tail(60), width="stretch")
    with tabs[1]:
        st.dataframe(engine.get_pnl_contribution(), width="stretch", hide_index=True)
    with tabs[2]:
        st.dataframe(engine.get_fills(), width="stretch", hide_index=True)
    with tabs[3]:
        st.dataframe(engine.get_orders(), width="stretch", hide_index=True)
    with tabs[4]:
        st.dataframe(engine.get_correlation_matrix(), width="stretch")


def page_cost_sensitivity():
    st.header("交易成本敏感性")
    st.caption("分析佣金、最低佣金、滑点和成交量参与率对收益的影响。")
    strategies = get_available_strategies()
    with st.sidebar:
        st.divider()
        st.caption("成本场景")
        code = st.text_input("股票代码", value="600519", key="cost_code")
        start, end = _date_range(st, st, key_prefix="cost")
        strategy_name = st.selectbox(
            "策略",
            [item["name"] for item in strategies],
            key="cost_strategy",
        )
        strategy_info = next(
            item for item in strategies if item["name"] == strategy_name
        )
        params = _strategy_param_widgets(strategy_info, "cost")
        commission = st.number_input(
            "基准佣金", min_value=0.0, value=0.00025,
            step=0.00005, format="%.5f", key="cost_commission",
        )
        slippage = st.number_input(
            "基准滑点", min_value=0.0, value=0.001,
            step=0.0005, format="%.4f", key="cost_slippage",
        )
        participation = st.slider(
            "基准参与率", 0.01, 1.0, 0.05, 0.01, key="cost_part",
        )
        run = st.button("运行敏感性分析", type="primary", width="stretch")
    if run:
        try:
            with st.spinner("正在运行成本场景..."):
                frame = load_market_data(code, start, end, freq="daily")
                report = cost_sensitivity_from_strategy(
                    frame,
                    strategy_info["strategy_key"],
                    params,
                    base_kwargs={
                        "code": code,
                        "commission": commission,
                        "slippage": slippage,
                        "participation_rate": participation,
                    },
                )
                st.session_state["cost_report"] = report
        except Exception as exc:
            st.error(f"成本分析失败：{exc}")
    report = st.session_state.get("cost_report")
    if report is None:
        st.info("设置成本基准后运行分析。")
        return
    summary = summarize_cost_sensitivity(report)
    cols = st.columns(4)
    cols[0].metric("基准收益", _pct(summary["基准收益率"]))
    cols[1].metric("零成本收益", _pct(summary["零成本收益率"]))
    cols[2].metric("基准成本拖累", _pct(summary["基准成本拖累"]))
    cols[3].metric("最差场景", summary["最差场景"])
    st.dataframe(report, width="stretch", hide_index=True)
    figure = go.Figure()
    figure.add_trace(go.Bar(
        x=report["场景"],
        y=report["相对零成本拖累"],
        marker_color="#d03b3b",
    ))
    figure.update_layout(
        title="相对零交易成本的收益拖累",
        yaxis_tickformat=".2%",
        xaxis_tickangle=-35,
        margin=dict(l=30, r=20, t=50, b=100),
    )
    st.plotly_chart(figure, width="stretch", config=PLOT_CONFIG)


def page_validation():
    st.header("稳健性与过拟合验证")
    st.caption("样本内外、滚动、Walk-forward、参数稳定性、蒙特卡洛和基准比较。")
    strategies = get_available_strategies()
    with st.sidebar:
        st.divider()
        st.caption("验证设置")
        code = st.text_input("股票代码", value="600519", key="validation_code")
        start, end = _date_range(st, st, key_prefix="validation")
        strategy_name = st.selectbox(
            "策略",
            [item["name"] for item in strategies],
            key="validation_strategy",
        )
        strategy_info = next(
            item for item in strategies if item["name"] == strategy_name
        )
        ranges = _validation_range_widgets(strategy_info)
        split_ratio = st.slider(
            "样本内比例", 0.5, 0.85, 0.7, 0.05, key="validation_split",
        )
        splits = st.number_input(
            "Walk-forward 折数", min_value=2, max_value=8, value=3,
            step=1, key="validation_splits",
        )
        mc_runs = st.number_input(
            "蒙特卡洛次数", min_value=100, max_value=5000, value=300,
            step=100, key="validation_mc",
        )
        run = st.button("运行稳健性验证", type="primary", width="stretch")
    if run:
        try:
            with st.spinner("正在执行样本内外、Walk-forward 和蒙特卡洛..."):
                frame = load_market_data(code, start, end, freq="daily")
                report = evaluate_strategy_robustness(
                    frame,
                    strategy_info["strategy_key"],
                    ranges,
                    metric="夏普比率",
                    method="grid",
                    config=ValidationConfig(
                        in_sample_ratio=split_ratio,
                        n_splits=int(splits),
                        monte_carlo_runs=int(mc_runs),
                        top_k=8,
                    ),
                    optimizer_kwargs={"max_workers": 1, "code": code},
                    experiment_name=f"{code} {strategy_name} 稳健性验证",
                )
                st.session_state["validation_report"] = report
        except Exception as exc:
            st.error(f"稳健性验证失败：{exc}")
    report = st.session_state.get("validation_report")
    if report is None:
        st.info("在左侧配置参数范围和验证折数后开始验证。")
        return
    risk_color = {
        "LOW": "#0ca30c",
        "MEDIUM": "#eb6834",
        "HIGH": "#d03b3b",
    }.get(report.overfitting["risk_level"], "#52514e")
    cols = st.columns(5)
    cols[0].metric("过拟合风险", report.overfitting["risk_level"])
    cols[1].metric("稳健性分数", _num(report.overfitting["robustness_score"]))
    cols[2].metric("样本内指标", _num(report.in_sample["metric_value"]))
    cols[3].metric("样本外指标", _num(report.out_sample["metric_value"]))
    cols[4].metric("基准超额", _pct(report.benchmark["excess_return"]))
    st.markdown(
        f"<div style='padding:0.75rem 1rem;border-left:4px solid {risk_color};"
        f"background:{'#f0f8f0' if report.overfitting['risk_level']=='LOW' else '#fff4ed' if report.overfitting['risk_level']=='MEDIUM' else '#fff0f0'};'>"
        f"<b>主要风险：</b>{'；'.join(report.overfitting['reasons'])}</div>",
        unsafe_allow_html=True,
    )
    st.caption(f"实验 ID：{report.experiment_id}")
    tabs = st.tabs(["样本内外", "Walk-forward", "参数稳定性", "蒙特卡洛", "基准比较"])
    with tabs[0]:
        frame = pd.DataFrame([
            {"阶段": "样本内", **report.in_sample["metrics"]},
            {"阶段": "样本外", **report.out_sample["metrics"]},
        ])
        st.dataframe(frame, width="stretch", hide_index=True)
    with tabs[1]:
        folds = pd.DataFrame(report.walk_forward.get("folds", []))
        st.dataframe(folds, width="stretch", hide_index=True)
        st.metric("正收益折数比例", _pct(report.walk_forward["positive_fold_ratio"]))
    with tabs[2]:
        st.metric("参数稳定性", _num(report.parameter_stability["stability_score"]))
        st.json({
            key: value for key, value in report.parameter_stability.items()
            if key != "top_params"
        })
        st.dataframe(
            pd.DataFrame(report.parameter_stability.get("top_params", [])),
            width="stretch",
            hide_index=True,
        )
    with tabs[3]:
        mc = report.monte_carlo
        st.metric("正收益概率", _pct(mc["probability_positive"]))
        st.metric("回撤超过 20% 概率", _pct(mc["probability_drawdown_over_20"]))
        st.json({
            "期末收益分布": mc.get("terminal_return", {}),
            "最大回撤分布": mc.get("maximum_drawdown", {}),
        })
    with tabs[4]:
        benchmark = pd.DataFrame([
            {"对象": "策略", **report.benchmark["strategy"]},
            {"对象": "买入持有", **report.benchmark["buy_and_hold"]},
        ])
        st.dataframe(benchmark, width="stretch", hide_index=True)


def page_experiments():
    st.header("实验台账")
    st.caption("所有单股、组合、参数寻优和稳健性实验的输入指纹与结果。")
    try:
        records = ExperimentStore().list()
    except Exception as exc:
        st.error(f"读取实验失败：{exc}")
        return
    if not records:
        st.info("还没有实验记录。")
        return
    rows = [{
        "实验 ID": item.experiment_id,
        "类型": item.kind,
        "名称": item.name,
        "创建时间": item.created_at,
        "代码版本": item.code_version,
        "复现指纹": item.reproducibility_key,
        "结果哈希": item.result_hash,
    } for item in records]
    table = pd.DataFrame(rows)
    kind = st.selectbox("筛选类型", ["全部"] + sorted(table["类型"].unique()))
    if kind != "全部":
        table = table[table["类型"] == kind]
    st.dataframe(table, width="stretch", hide_index=True)
    selected = st.selectbox("查看实验", table["实验 ID"].tolist())
    record = next(item for item in records if item.experiment_id == selected)
    st.json(record.to_dict())
    store = ExperimentStore()
    for name in ("results", "signals", "target_weights", "actual_weights", "positions"):
        if name in record.artifacts:
            try:
                frame = pd.read_csv(store.artifact_path(record, name))
                st.subheader(name)
                st.dataframe(frame.head(200), width="stretch", hide_index=True)
            except Exception:
                pass


def _strategy_param_widgets(strategy_info: dict, prefix: str) -> dict:
    params = {}
    for spec in strategy_info["parameter_schema"]:
        key = f"{prefix}_{spec['name']}"
        label = spec["description"].split("，")[0]
        if spec["kind"] == "bool":
            params[spec["name"]] = st.checkbox(label, value=spec["default"], key=key)
        elif spec["kind"] == "int":
            params[spec["name"]] = st.number_input(
                label,
                value=int(spec["default"]),
                min_value=int(spec["minimum"] or 1),
                max_value=int(spec["maximum"]) if spec["maximum"] is not None else None,
                step=int(spec["step"] or 1),
                key=key,
            )
        elif spec["kind"] == "float":
            params[spec["name"]] = st.number_input(
                label,
                value=float(spec["default"]),
                min_value=float(spec["minimum"] or 0.0),
                max_value=float(spec["maximum"]) if spec["maximum"] is not None else None,
                step=float(spec["step"] or 0.1),
                format="%.4f",
                key=key,
            )
        elif spec["choices"]:
            options = list(spec["choices"])
            params[spec["name"]] = st.selectbox(
                label,
                options,
                index=options.index(spec["default"]) if spec["default"] in options else 0,
                key=key,
            )
        else:
            params[spec["name"]] = st.text_input(
                label, value=str(spec["default"]), key=key
            )
    return params


def _validation_range_widgets(strategy_info: dict) -> dict:
    ranges = {}
    for spec in strategy_info["parameter_schema"]:
        if not spec.get("optimize", True) or spec["kind"] not in {"int", "float"}:
            continue
        default = spec["default"]
        step = spec.get("step") or (1 if spec["kind"] == "int" else 0.1)
        minimum = default - step * 2
        maximum = default + step * 3
        if spec["kind"] == "int":
            minimum = max(int(spec.get("minimum") or 1), int(minimum))
            maximum = max(minimum + 1, int(maximum))
        else:
            minimum = max(float(spec.get("minimum") or 0.0), float(minimum))
            maximum = max(minimum + step, float(maximum))
        cols = st.columns(3)
        ranges[spec["name"]] = {
            "min": cols[0].number_input(
                f"{spec['name']} 最小", value=minimum, step=step,
                key=f"validation_{spec['name']}_min",
            ),
            "max": cols[1].number_input(
                f"{spec['name']} 最大", value=maximum, step=step,
                key=f"validation_{spec['name']}_max",
            ),
            "step": cols[2].number_input(
                f"{spec['name']} 步长", value=step, min_value=step,
                step=step, key=f"validation_{spec['name']}_step",
            ),
        }
    return ranges


def _create_signal(strategy_name: str, params: dict, frame):
    from strategies.factory import create_strategy
    return create_strategy(strategy_name, **params).generate_signal_output(frame)


def _date_range(column_a, column_b, key_prefix: str = "dates"):
    today = date.today()
    default_start = today - timedelta(days=3 * 365)
    start = column_a.date_input(
        "开始日期", value=default_start, key=f"{key_prefix}_start"
    )
    end = column_b.date_input(
        "结束日期", value=today, key=f"{key_prefix}_end"
    )
    return start, end


def _show_snapshot_table(code=None):
    snapshots = list_snapshots(symbol=code)
    if not snapshots:
        st.info("没有可显示的数据快照。")
        return
    rows = [{
        "快照 ID": item.snapshot_id,
        "股票代码": item.symbol,
        "频率": item.frequency,
        "复权": item.adjust,
        "数据源": item.provider,
        "行数": item.rows,
        "数据范围": f"{item.actual_start} ~ {item.actual_end}",
        "质量": item.quality.get("status"),
        "创建时间": item.created_at,
    } for item in snapshots]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def _price_figure(frame, title):
    plot = frame.copy()
    if "date" in plot.columns:
        x = pd.to_datetime(plot["date"])
    else:
        x = plot.index
    figure = go.Figure()
    figure.add_trace(go.Candlestick(
        x=x,
        open=plot["open"],
        high=plot["high"],
        low=plot["low"],
        close=plot["close"],
        name="K 线",
    ))
    figure.update_layout(
        title=title,
        height=420,
        xaxis_rangeslider_visible=False,
        margin=dict(l=30, r=20, t=50, b=20),
    )
    return figure


def _equity_figure(equity):
    figure = go.Figure()
    figure.add_trace(go.Scatter(
        x=equity.index,
        y=equity.values,
        mode="lines",
        name="净值",
        line=dict(color="#2a78d6", width=2),
    ))
    figure.update_layout(
        title="账户净值",
        height=360,
        margin=dict(l=30, r=20, t=50, b=20),
        yaxis_title="资产（元）",
    )
    return figure


def _drawdown_figure(equity):
    drawdown = equity / equity.cummax() - 1
    figure = go.Figure()
    figure.add_trace(go.Scatter(
        x=drawdown.index,
        y=drawdown.values,
        mode="lines",
        name="回撤",
        fill="tozeroy",
        line=dict(color="#d03b3b", width=2),
    ))
    figure.update_layout(
        title="回撤",
        height=360,
        margin=dict(l=30, r=20, t=50, b=20),
        yaxis_tickformat=".0%",
    )
    return figure


def _pct(value):
    if value is None:
        return "—"
    return f"{float(value):.2%}"


def _num(value):
    if value is None:
        return "—"
    value = float(value)
    if not np.isfinite(value):
        return "∞" if value > 0 else "-∞"
    return f"{value:.4f}"


def _apply_style():
    st.markdown(
        """
        <style>
        [data-testid="stMainBlockContainer"],
        section.main .block-container {
            padding-top: 4.5rem !important;
            padding-bottom: 2.5rem;
        }
        [data-testid="stSidebar"] .block-container {
            padding-top: 2rem;
        }
        [data-testid="stHeader"] {
            background: rgba(255, 255, 255, 0.9);
            backdrop-filter: blur(8px);
        }
        [data-testid="stMetric"] {
            border-top: 2px solid #d9d9d4;
            padding-top: .55rem;
            overflow: visible;
        }
        [data-testid="stMetricValue"],
        [data-testid="stMetricLabel"] {
            overflow: visible !important;
            text-overflow: clip !important;
            white-space: normal !important;
            line-height: 1.25 !important;
        }
        h1, h2, h3 {
            line-height: 1.3 !important;
            overflow: visible !important;
        }
        [data-testid="stSidebar"] {
            border-right: 1px solid #e1e0d9;
        }
        [data-testid="stSidebar"] p,
        [data-testid="stSidebar"] label {
            overflow: visible !important;
            line-height: 1.45 !important;
        }
        @media (max-width: 900px) {
            [data-testid="stMainBlockContainer"],
            section.main .block-container {
                padding-top: 5rem !important;
            }
            .quant-env-band {
                flex-direction: column;
                align-items: flex-start !important;
            }
            .quant-env-note {
                margin-left: 0 !important;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
