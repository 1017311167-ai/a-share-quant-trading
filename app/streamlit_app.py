"""Streamlit 交互界面 —— A股量化回测

运行方式:
    python main.py
或
    streamlit run app/streamlit_app.py

页面功能:
    侧边栏：股票代码、回测起止日期、策略选择、策略参数、初始资金、佣金滑点、
            A股交易规则开关、风控限额、参数寻优
    主界面：绩效概览卡片、净值曲线、回撤曲线、持仓买卖点、绩效指标表、
            月度收益矩阵、交易明细、参数寻优结果、完整绩效报告、CSV 导出
"""

import os
import sys

# 保证直接运行本文件（python app/streamlit_app.py --test）时能找到 core / strategies / utils 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.optimizer import METRIC_FMT, METRICS, build_heatmap, optimize, ranges_to_grid
from core.risk_analysis import analyze_portfolio, trades_table
from research.experiments import CostAssumptions, run_backtest_experiment
from strategies import (BollStrategy, DoubleMAStrategy, MomentumStrategy,
                        RSIStrategy, TurtleStrategy)
from strategies.factory import create_strategy, get_available_strategies
from utils.data_loader import load_daily_data

# 策略选择框里 key（类名）对应的策略类
_CLASS_BY_NAME = {s.__name__: s for s in
                  (DoubleMAStrategy, RSIStrategy, BollStrategy,
                   MomentumStrategy, TurtleStrategy)}


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def _pct(x, digits: int = 2) -> str:
    """小数转百分比字符串；None -> "—" """
    return "—" if x is None else f"{x * 100:.{digits}f}%"


def _num(x, digits: int = 4) -> str:
    """数字转字符串；None -> "—"，inf -> "∞" """
    if x is None:
        return "—"
    if x == float("inf"):
        return "∞"
    return f"{x:.{digits}f}"


# ---------------------------------------------------------------------------
# 回测流程（与界面解耦，方便测试）
# ---------------------------------------------------------------------------

def run_pipeline(code, start, end, strategy, params, init_cash, commission, slippage,
                 rf: float = 0.0, t_plus_1: bool = True, price_limit: bool = True,
                 stamp_tax: bool = True, transfer_fee: bool = True):
    """执行完整回测流程：下载数据 -> 生成信号 -> 回测 -> 风控分析

    参数:
        code:       股票代码（6 位数字）
        start/end:  起止日期
        strategy:   策略类（如 DoubleMAStrategy）或策略名（如 "双均线"）
        params:     策略参数字典
        init_cash:  初始资金（元）
        commission: 佣金费率（小数，万 2.5 = 0.00025）
        slippage:   滑点（小数）
        rf:         无风险利率（年化小数）
        t_plus_1:   T+1：信号次日执行（默认开）
        price_limit: 涨跌停限制，涨停买不进/跌停卖不出（默认开）
        stamp_tax:  印花税（卖出收 0.05%，默认开）
        transfer_fee: 过户费（买卖双向收 0.001%，默认开）

    返回:
        {"df": 行情数据, "strategy": 策略对象, "engine": 已运行的回测引擎,
         "metrics": 引擎指标, "risk_metrics": 风控绩效指标,
         "analyzer": 风险分析器, "trades_df": 中文列名交易明细}
    """
    df = load_daily_data(code, start, end)
    if df.empty:
        raise ValueError("没有下载到行情数据，请检查股票代码和日期区间")

    run = run_backtest_experiment(
        df,
        strategy,
        params,
        code=code,
        symbol=code,
        costs=CostAssumptions(
            init_cash=init_cash,
            commission=commission,
            slippage=slippage,
            stamp_tax=stamp_tax,
            transfer_fee=transfer_fee,
            t_plus_1=t_plus_1,
            price_limit=price_limit,
            rf=rf,
        ),
        name=f"{code} {strategy} 网页回测",
    )
    strategy_obj = run["strategy"]
    engine = run["engine"]
    analyzer = analyze_portfolio(engine.portfolio, rf=rf)
    return {
        "df": df,
        "strategy": strategy_obj,
        "engine": engine,
        "metrics": run["metrics"]["engine"],
        "risk_metrics": analyzer.compute_metrics(),
        "analyzer": analyzer,
        "trades_df": build_trades_df(engine.portfolio),
        "experiment_id": run["experiment_id"],
        "reproducibility_key": run["reproducibility_key"],
    }


def build_trades_df(portfolio):
    """把 vectorbt 交易记录转成中文列名的交易明细表"""
    return trades_table(portfolio)


def _style_fig(fig, title, ticksuffix: str = ""):
    """统一图表样式：浅色背景、浅灰网格、日期悬浮框"""
    fig.update_layout(
        title=dict(text=title, font=dict(size=16, color="#0b0b0b")),
        paper_bgcolor="#fcfcfb",
        plot_bgcolor="#fcfcfb",
        font=dict(color="#52514e", size=12),
        margin=dict(l=40, r=20, t=50, b=40),
        hovermode="x unified",
        height=380,
    )
    fig.update_xaxes(gridcolor="#e1e0d9", linecolor="#c3c2b7", zeroline=False)
    fig.update_yaxes(gridcolor="#e1e0d9", linecolor="#c3c2b7",
                     zeroline=True, zerolinecolor="#c3c2b7", ticksuffix=ticksuffix)


def build_drawdown_fig(drawdown_series):
    """回撤曲线图（A股习惯：亏损用绿色），标注最大回撤点"""
    dd = drawdown_series * 100
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=dd.index, y=dd.values, mode="lines",
        line=dict(color="#0ca30c", width=2),
        fill="tozeroy", fillcolor="rgba(12, 163, 12, 0.12)",
        hovertemplate="%{x|%Y-%m-%d}<br>回撤 %{y:.2f}%<extra></extra>",
    ))
    i_min = dd.values.argmin()
    fig.add_trace(go.Scatter(
        x=[dd.index[i_min]], y=[dd.values[i_min]], mode="markers",
        marker=dict(color="#0ca30c", size=8),
        showlegend=False, hoverinfo="skip",
    ))
    fig.add_annotation(
        x=dd.index[i_min], y=dd.values[i_min], yshift=-16,
        text=f"最大回撤 {dd.values[i_min]:.2f}%",
        showarrow=False, font=dict(color="#0ca30c", size=12),
    )
    _style_fig(fig, title="回撤曲线", ticksuffix="%")
    return fig


def _monthly_grid(monthly, yearly) -> pd.DataFrame:
    """月度收益矩阵：行 = 年份，列 = 1~12 月 + 全年"""
    years = sorted(monthly.index.year.unique())
    year_map = dict(zip(yearly.index.year, yearly))
    data = {}
    for y in years:
        row = []
        for mm in range(1, 13):
            sub = monthly[(monthly.index.year == y) & (monthly.index.month == mm)]
            row.append(f"{sub.iloc[0] * 100:+.2f}" if len(sub) else "—")
        full = year_map.get(y)
        row.append(f"{full * 100:+.2f}" if full is not None else "—")
        data[y] = row
    grid = pd.DataFrame.from_dict(data, orient="index",
                                  columns=[f"{m}月" for m in range(1, 13)] + ["全年"])
    return grid.reset_index().rename(columns={"index": "年份"})


# ---------------------------------------------------------------------------
# CSV 导出（带 BOM，Excel 直接打开中文不乱码）
# ---------------------------------------------------------------------------

def metrics_to_csv(metrics, risk_metrics) -> bytes:
    rows = [
        ("累计收益率", risk_metrics["累计收益率"]),
        ("年化收益率", risk_metrics["年化收益率"]),
        ("年化波动率", risk_metrics["年化波动率"]),
        ("夏普比率", risk_metrics["夏普比率"]),
        ("卡玛比率", risk_metrics["卡玛比率"]),
        ("最大回撤", risk_metrics["最大回撤"]),
        ("胜率", risk_metrics["胜率"]),
        ("盈亏比", risk_metrics["盈亏比"]),
        ("总交易次数", risk_metrics["总交易次数"]),
        ("总手续费（元）", metrics["总手续费"]),
        ("期末总资产", metrics["期末总资产"]),
        ("基准收益率", metrics["基准收益率"]),
    ]
    return pd.DataFrame(rows, columns=["指标", "数值"]).to_csv(index=False).encode("utf-8-sig")


def equity_to_csv(equity) -> bytes:
    out = equity.to_frame("净值").reset_index()
    out.columns = ["日期", "净值"]
    return out.to_csv(index=False).encode("utf-8-sig")


def trades_to_csv(trades_df) -> bytes:
    return trades_df.to_csv(index=False).encode("utf-8-sig")


# ---------------------------------------------------------------------------
# 界面
# ---------------------------------------------------------------------------

def _param_widget(label, key, default):
    """根据默认值类型生成对应的策略参数控件"""
    if key == "exit_mode":
        return st.selectbox(
            label, options=["mid", "lower"],
            format_func=lambda v: "跌破中轨卖出" if v == "mid" else "跌破下轨卖出")
    if isinstance(default, bool):
        return st.checkbox(label, value=default)
    if isinstance(default, int):
        return st.number_input(label, value=default, min_value=1, step=1)
    if isinstance(default, float):
        step = 0.5 if default < 10 else 1.0
        return st.number_input(label, value=float(default), min_value=0.0, step=step)
    return st.text_input(label, value=str(default))


def _default_range(default):
    """按参数默认值给出一个合理的寻优范围，返回 (最小, 最大, 步长)"""
    if isinstance(default, int):
        return max(1, default - 5), default + 10, 5
    if default < 10:  # 如布林带 k=2.0
        return max(0.1, default - 1.0), default + 1.0, 0.5
    return max(1.0, default - 10.0), default + 10.0, 5.0  # 如 RSI 阈值 30/70


def _params_str(params: dict) -> str:
    """参数字典转展示字符串：{"fast": 5, "slow": 20} -> "fast=5、slow=20" """
    return "、".join(f"{k}={v:g}" if isinstance(v, float) else f"{k}={v}"
                     for k, v in params.items())


def main():
    st.set_page_config(page_title="A股量化回测", page_icon="📈", layout="wide")

    # ---------- 侧边栏 ----------
    with st.sidebar:
        st.title("📈 回测设置")

        code = st.text_input("股票代码", value="600519",
                             help="6 位数字，如 600519（贵州茅台）、000001（平安银行）")
        today = date.today()
        start, end = st.date_input(
            "回测区间",
            value=(today - timedelta(days=3 * 365), today),
            min_value=date(1990, 1, 1), max_value=today,
            help="开始日期要早于结束日期",
        )

        st.divider()
        strategies = get_available_strategies()
        selected = st.selectbox(
            "选择策略",
            options=strategies,
            format_func=lambda s: s["name"],
        )
        params = {}
        for key, default in selected["default_params"].items():
            label = selected["params_help"].get(key, key)
            params[key] = _param_widget(label, key, default)
        st.caption("修改参数后点击「运行回测」生效")

        st.divider()
        init_cash = st.number_input("初始资金（元）", value=1_000_000, min_value=10_000,
                                    step=100_000, help="回测起始资金")
        commission = st.number_input("佣金费率", value=0.00025, min_value=0.0,
                                     step=0.00005, format="%.5f",
                                     help="A 股默认万 2.5 = 0.00025")
        slippage = st.number_input("滑点", value=0.001, min_value=0.0,
                                   step=0.0005, format="%.4f",
                                   help="按成交金额的 0.1% 计（默认）")

        with st.expander("A股交易规则", expanded=True):
            t_plus_1_on = st.checkbox("T+1：信号次日执行", value=True,
                                      help="收盘产生信号、下一个交易日成交，与真实交易一致")
            limit_on = st.checkbox("涨跌停限制", value=True,
                                   help="涨停日买不进、跌停日卖不出，信号自动顺延（最多 5 天）")
            tax_on = st.checkbox("印花税 + 过户费", value=True,
                                 help="卖出收印花税 0.05%，买卖双向收过户费 0.001%")

        with st.expander("风控限额（可选）"):
            position_limit = st.number_input("单票仓位上限（%）", value=100.0,
                                             min_value=1.0, max_value=100.0, step=5.0,
                                             help="持仓市值占总资产比例超过该值会在报告中标记")
            daily_loss_limit = st.number_input("单日亏损上限（%）", value=100.0,
                                               min_value=1.0, max_value=100.0, step=1.0,
                                               help="单日跌幅超过该值会在报告中标记")

        run_clicked = st.button("🚀 运行回测", type="primary", width="stretch")

        with st.expander("🔍 参数寻优"):
            opt_metric = st.selectbox("寻优目标", METRICS, index=2,
                                      help="按该指标挑最优参数；最大回撤越小越好，其余越大越好")
            opt_ranges = {}
            numeric_params = 0
            for key, default in selected["default_params"].items():
                if not isinstance(default, (int, float)) or isinstance(default, bool):
                    continue  # 非数值参数（如 exit_mode）不参与寻优
                numeric_params += 1
                lo_default, hi_default, step_default = _default_range(default)
                wkey = f"opt_{selected['key']}_{key}"
                c1, c2, c3 = st.columns(3)
                lo = c1.number_input(f"{key} 最小", value=lo_default, step=step_default,
                                     key=f"{wkey}_min")
                hi = c2.number_input(f"{key} 最大", value=hi_default, step=step_default,
                                     key=f"{wkey}_max")
                stp = c3.number_input(f"{key} 步长", value=step_default, step=step_default,
                                      min_value=1 if isinstance(default, int) else 0.1,
                                      key=f"{wkey}_step")
                opt_ranges[key] = {"min": lo, "max": hi, "step": stp}
            if numeric_params == 0:
                st.caption("该策略没有可寻优的数值参数")
            opt_clicked = st.button("开始寻优", width="stretch")
            if opt_clicked:
                if start >= end:
                    st.error("结束日期必须晚于开始日期")
                else:
                    try:
                        grid = ranges_to_grid(opt_ranges)
                        with st.spinner(f"正在遍历参数组合逐个回测，请稍候……"):
                            out = optimize(
                                load_daily_data(code, start, end),
                                _CLASS_BY_NAME[selected["key"]], grid,
                                metric=opt_metric,
                                engine_kwargs={"init_cash": init_cash,
                                               "commission": commission,
                                               "slippage": slippage, "code": code,
                                               "t_plus_1": t_plus_1_on,
                                               "price_limit": limit_on,
                                               "stamp_tax": tax_on,
                                               "transfer_fee": tax_on})
                        st.session_state["opt_result"] = {
                            "strategy_key": selected["key"],
                            "strategy_name": selected["name"],
                            "metric": opt_metric,
                            "param_names": list(grid.keys()),
                            "out": out,
                        }
                    except ValueError as e:
                        st.error(f"寻优失败：{e}")

    # ---------- 主界面 ----------
    st.title("📈 A股量化回测")
    st.caption("数据来自 AKShare 免费接口 · 整手交易（100股） · T+1/涨跌停/印花税按左侧开关生效")

    # 点击运行：执行回测并保存结果
    if run_clicked:
        if start >= end:
            st.sidebar.error("结束日期必须晚于开始日期")
        else:
            try:
                with st.spinner("正在下载行情数据并回测，请稍候……"):
                    result = run_pipeline(code, start, end, _CLASS_BY_NAME[selected["key"]],
                                          params, init_cash, commission, slippage,
                                          t_plus_1=t_plus_1_on, price_limit=limit_on,
                                          stamp_tax=tax_on, transfer_fee=tax_on)
                rules_on = [r for r, on in (("T+1", t_plus_1_on), ("涨跌停", limit_on),
                                            ("印花税", tax_on)) if on]
                st.session_state["result"] = result
                st.session_state["run_info"] = (
                    f"{code} · {start} ~ {end} · {selected['name']} · 参数 {_params_str(params)}"
                    f" · 初始资金 {init_cash:,.0f} 元"
                    f" · 规则：{('、'.join(rules_on)) if rules_on else '无A股规则'}")
            except Exception as e:
                st.error(f"回测失败：{e}")

    result = st.session_state.get("result")
    if result is None:
        st.info(
            "👈 在左侧设置股票、日期和策略，点击「运行回测」开始。\n\n"
            "**三步完成一次回测**：\n"
            "1. 输入股票代码（如 600519 贵州茅台）\n"
            "2. 选择回测区间和策略（双均线 / RSI / 布林带），可调整策略参数\n"
            "3. 点击「运行回测」，图表和指标会显示在这里"
        )
        return

    st.caption(f"回测对象：{st.session_state['run_info']}")

    m = result["metrics"]
    rm = result["risk_metrics"]

    # 绩效概览卡片
    st.subheader("绩效概览")
    cards = [
        ("累计收益率", _pct(rm["累计收益率"])),
        ("年化收益率", _pct(rm["年化收益率"])),
        ("最大回撤", _pct(rm["最大回撤"])),
        ("夏普比率", f"{rm['夏普比率']:.2f}"),
        ("胜率", _pct(rm["胜率"])),
        ("总交易次数", str(rm["总交易次数"])),
    ]
    for col, (label, value) in zip(st.columns(6), cards):
        col.metric(label, value)

    # 图表
    figs = result["engine"].plot()
    st.subheader("净值曲线")
    st.plotly_chart(figs["equity"], width="stretch")
    st.subheader("回撤曲线")
    st.plotly_chart(build_drawdown_fig(result["analyzer"].drawdown_series()),
                    width="stretch")

    # 指标明细 + 年度收益
    left, right = st.columns(2)
    with left:
        st.subheader("绩效指标")
        table_rows = [
            ("累计收益率", _pct(rm["累计收益率"])),
            ("年化收益率", _pct(rm["年化收益率"])),
            ("年化波动率", _pct(rm["年化波动率"])),
            ("夏普比率", _num(rm["夏普比率"])),
            ("卡玛比率", _num(rm["卡玛比率"])),
            ("最大回撤", _pct(rm["最大回撤"])),
            ("胜率", _pct(rm["胜率"])),
            ("盈亏比", _num(rm["盈亏比"])),
            ("总交易次数", str(rm["总交易次数"])),
            ("总手续费（元）", f"{m['总手续费']:,.0f}"),
            ("期末总资产（元）", f"{m['期末总资产']:,.0f}"),
            ("基准收益率", _pct(m["基准收益率"])),
        ]
        st.dataframe(pd.DataFrame(table_rows, columns=["指标", "数值"]),
                     hide_index=True, width="stretch")
    with right:
        st.subheader("年度收益")
        yearly = result["analyzer"].yearly_returns()
        if yearly.empty:
            st.write("数据不足一年")
        else:
            st.dataframe(
                pd.DataFrame(
                    {"年份": [str(d.year) for d in yearly.index],
                     "收益率": [_pct(v) for v in yearly.values]}),
                hide_index=True, width="stretch")

    # 月度收益矩阵
    st.subheader("月度收益（%）")
    st.dataframe(_monthly_grid(result["analyzer"].monthly_returns(), yearly),
                 hide_index=True, width="stretch")

    # 持仓与买卖点
    st.subheader("持仓与买卖点")
    st.plotly_chart(figs["position"], width="stretch")

    # 交易明细
    st.subheader("交易明细")
    trades_df = result["trades_df"]
    st.dataframe(
        trades_df, hide_index=True, width="stretch",
        column_config={
            "买入价": st.column_config.NumberColumn(format="%.2f"),
            "卖出价": st.column_config.NumberColumn(format="%.2f"),
            "盈亏（元）": st.column_config.NumberColumn(format="%.2f"),
            "收益率": st.column_config.NumberColumn(format="percent"),
        },
    )
    st.caption(f"共 {len(trades_df)} 笔交易，状态列标记未平仓的交易")

    # 参数寻优结果（与当前所选策略匹配时显示）
    opt = st.session_state.get("opt_result")
    if opt and opt["strategy_key"] == selected["key"]:
        st.divider()
        st.subheader(f"🔍 参数寻优（目标：{opt['metric']}）")
        out = opt["out"]
        best = out["best"]
        best_params = {k: best[k] for k in opt["param_names"]}
        fmt = METRIC_FMT.get(opt["metric"], ".2f")
        st.success(
            f"最优参数：{_params_str(best_params)} → {opt['metric']} {best[opt['metric']]:{fmt}}"
            f"（跳过无效组合 {out['skipped']} 组）")
        if len(opt["param_names"]) == 2:
            st.plotly_chart(build_heatmap(out["results"], opt["param_names"][0],
                                          opt["param_names"][1], opt["metric"]),
                            width="stretch")
        st.dataframe(
            out["results"].head(20), hide_index=True, width="stretch",
            column_config={
                col: st.column_config.NumberColumn(format=fm)
                for col, fm in METRIC_FMT.items() if col in out["results"].columns
            })
        if st.button("🚀 用最优参数回测", width="stretch"):
            try:
                with st.spinner("正在用最优参数回测，请稍候……"):
                    result2 = run_pipeline(code, start, end,
                                           _CLASS_BY_NAME[selected["key"]],
                                           best_params, init_cash, commission, slippage,
                                           t_plus_1=t_plus_1_on, price_limit=limit_on,
                                           stamp_tax=tax_on, transfer_fee=tax_on)
                st.session_state["result"] = result2
                st.session_state["run_info"] = (
                    f"{code} · {start} ~ {end} · {selected['name']}"
                    f" · 参数 {_params_str(best_params)}（寻优最优）"
                    f" · 初始资金 {init_cash:,.0f} 元")
                st.rerun()
            except Exception as e:
                st.error(f"回测失败：{e}")

    # 完整报告
    with st.expander("📄 完整绩效报告"):
        report_text = result["analyzer"].report(risk_limits={
            "position_limit_pct": position_limit,
            "daily_loss_limit_pct": daily_loss_limit,
        })
        st.code(report_text, language=None)

    # CSV 导出
    with st.expander("⬇️ 导出数据（CSV）"):
        c1, c2, c3 = st.columns(3)
        c1.download_button("下载绩效指标", data=metrics_to_csv(m, rm),
                           file_name=f"{code}_指标.csv", mime="text/csv",
                           width="stretch")
        c2.download_button("下载净值曲线",
                           data=equity_to_csv(result["engine"].portfolio.value()),
                           file_name=f"{code}_净值.csv", mime="text/csv",
                           width="stretch")
        c3.download_button("下载交易明细", data=trades_to_csv(trades_df),
                           file_name=f"{code}_交易明细.csv", mime="text/csv",
                           width="stretch")
        st.caption("CSV 带 BOM 头，可用 Excel 直接打开，中文不乱码")


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

def run_test():
    """测试：验证回测流程与界面辅助函数（不依赖浏览器）

    运行方式（在项目根目录下）：
        python app/streamlit_app.py --test
    """
    print("===== 界面回测流程测试开始 =====")
    end = date.today()
    start = end - timedelta(days=3 * 365)

    # 1. 完整流程（双均线默认参数）
    result = run_pipeline("600519", start, end, DoubleMAStrategy,
                          {"fast": 5, "slow": 20}, 1_000_000, 0.00025, 0.001)
    assert set(result) >= {"df", "strategy", "engine", "metrics", "risk_metrics",
                           "analyzer", "trades_df"}, "返回结果缺少字段"
    assert result["risk_metrics"]["总交易次数"] > 0
    print(f"  ✓ 双均线完整流程：交易 {result['risk_metrics']['总交易次数']} 次，"
          f"累计 {result['risk_metrics']['累计收益率']:.2%}")

    # 2. 交易明细中文列名与状态
    t = result["trades_df"]
    assert list(t.columns) == ["买入日期", "卖出日期", "买入价", "卖出价", "数量（股）",
                               "盈亏（元）", "收益率", "状态"]
    assert set(t["状态"].unique()) <= {"已平仓", "持仓中"}
    print(f"  ✓ 交易明细：{len(t)} 行，中文列名与状态正确")

    # 3. 回撤图：纵轴全部 ≤ 0，已标注最大回撤
    fig = build_drawdown_fig(result["analyzer"].drawdown_series())
    assert isinstance(fig, go.Figure)
    assert (fig.data[0].y <= 1e-9).all(), "回撤曲线不应出现正值"
    assert len(fig.layout.annotations) == 1, "应标注最大回撤点"
    print("  ✓ 回撤图：纵轴全部 ≤ 0，最大回撤点已标注")

    # 4. 五个策略 + 自定义参数都跑通
    for strategy, params in [
        (RSIStrategy, {}),
        (BollStrategy, {}),
        (DoubleMAStrategy, {"fast": 10, "slow": 60}),
        (MomentumStrategy, {"momentum_period": 20, "threshold": 0.05, "exit_ma": 60}),
        (TurtleStrategy, {"entry_window": 20, "exit_window": 10}),
    ]:
        r = run_pipeline("600519", start, end, strategy, params, 500_000, 0.0003, 0.001)
        assert r["risk_metrics"]["总交易次数"] > 0, f"{strategy.name} 应有交易"
        print(f"  ✓ 策略 [{strategy.name}] 参数 {params}："
              f"交易 {r['risk_metrics']['总交易次数']} 次")

    # 5. 策略名（字符串）也可直接跑：走策略工厂
    r = run_pipeline("600519", start, end, "布林带突破", {}, 1_000_000, 0.00025, 0.001)
    assert isinstance(r["strategy"], BollStrategy)
    print("  ✓ 策略名字符串入口：'布林带突破' 经工厂创建成功")

    # 6. CSV 导出：带 BOM 且非空
    m, rm = result["metrics"], result["risk_metrics"]
    for csv_bytes, name in [
        (metrics_to_csv(m, rm), "绩效指标"),
        (equity_to_csv(result["engine"].portfolio.value()), "净值曲线"),
        (trades_to_csv(t), "交易明细"),
    ]:
        assert csv_bytes.startswith(b"\xef\xbb\xbf"), f"{name} CSV 缺少 BOM"
        assert len(csv_bytes) > 50, f"{name} CSV 内容过短"
    print("  ✓ CSV 导出：指标/净值/交易三个文件均带 BOM 且非空")

    # 7. 月度收益矩阵
    grid = _monthly_grid(result["analyzer"].monthly_returns(),
                         result["analyzer"].yearly_returns())
    assert list(grid.columns) == ["年份"] + [f"{m}月" for m in range(1, 13)] + ["全年"]
    assert len(grid) >= 3, "近 3 年应至少有 3 行"
    print(f"  ✓ 月度收益矩阵：{len(grid)} 行 × {len(grid.columns)} 列")

    # 8. 非法股票代码应报错
    try:
        run_pipeline("12345", start, end, DoubleMAStrategy,
                     {"fast": 5, "slow": 20}, 1_000_000, 0.00025, 0.001)
    except ValueError as e:
        print(f"  ✓ 非法股票代码报错：{e}")
    else:
        raise AssertionError("非法股票代码应该抛出 ValueError")

    # 9. A 股规则开关直通引擎（默认开 vs 全关，总手续费应下降）
    r_off = run_pipeline("600519", start, end, DoubleMAStrategy,
                         {"fast": 5, "slow": 20}, 1_000_000, 0.00025, 0.001,
                         t_plus_1=False, price_limit=False,
                         stamp_tax=False, transfer_fee=False)
    assert result["metrics"]["总手续费"] > r_off["metrics"]["总手续费"], \
        "开启规则后总手续费应更高（印花税 + 过户费）"
    print(f"  ✓ A 股规则开关：规则开总手续费 {result['metrics']['总手续费']:,.0f} 元 > "
          f"全关 {r_off['metrics']['总手续费']:,.0f} 元")

    # 10. 参数寻优（小网格）+ 热力图
    out = optimize(result["df"], DoubleMAStrategy, {"fast": [5, 10], "slow": [20, 60]},
                   metric="夏普比率", engine_kwargs={"init_cash": 1_000_000})
    assert len(out["results"]) == 4 and out["skipped"] == 0
    fig = build_heatmap(out["results"], "fast", "slow", "夏普比率")
    assert isinstance(fig.data[0], go.Heatmap)
    best_params = {k: out["best"][k] for k in ("fast", "slow")}
    print(f"  ✓ 参数寻优：4 组全有效，最优 {_params_str(best_params)}，热力图生成成功")

    print("===== 全部测试通过 =====")


if __name__ == "__main__":
    if "--test" in sys.argv:
        run_test()
    else:
        main()
