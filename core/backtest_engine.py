"""
基于 VectorBT 封装的 A 股回测引擎

用法:
    bt = BacktestEngine(df, entries, exits,
                        init_cash=1_000_000,   # 初始资金（元）
                        commission=0.00025,    # 佣金费率（买卖双向）
                        slippage=0.001,        # 滑点（按成交价比例）
                        trade_unit=100)        # 交易单位：每手股数（A股 100 股）
    bt.run()             # 执行回测
    bt.get_metrics()     # 获取指标字典
    bt.plot()            # 获取图表字典

交易规则（当前版本，默认全部启用，可用参数关闭）:
    - 每次买入为全仓：可用资金全部买入，按整手向下取整，剩余资金保留现金
    - 佣金买卖双向收取；滑点按价格比例
    - T+1：买卖信号统一顺延到下一交易日执行（收盘产生信号、次日成交）
    - 涨跌停：涨停日买不进、跌停日卖不出，信号顺延到可成交日（最多 limit_defer_max 天）
    - 印花税（卖出 0.05%）+ 过户费（双向 0.001%）
    注意：涨跌停用复权收盘价近似判断，除权除息日可能偶有误判（影响很小）；
    佣金最低 5 元暂未计（对小资金回测有影响，大资金可忽略）。

运行测试:
    python core/backtest_engine.py
"""

import os
import sys
import tempfile

# 保证直接运行本文件（python core/backtest_engine.py）时能找到 utils 等包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import plotly.graph_objects as go

# 受限运行环境中 site-packages 和用户缓存目录可能不可写。Numba 必须能定位
# 可写的缓存目录，否则 vectorbt 在导入阶段就会失败。
os.environ.setdefault(
    "NUMBA_CACHE_DIR",
    os.path.join(tempfile.gettempdir(), "abacktest_numba_cache"),
)

try:
    import vectorbt as vbt
except ImportError:  # vectorbt 未安装时延迟报错，给出更友好的提示
    vbt = None

from utils.config import STAMP_TAX_RATE, TRANSFER_FEE_RATE, get_price_limit

# ---- 常量 ----

_REQUIRED_COLUMNS = ["open", "high", "low", "close", "volume"]

# 图表配色与样式（遵循项目可视化规范）
_LINE_BLUE = "#2a78d6"      # 主系列：策略净值 / 持仓
_LINE_ORANGE = "#eb6834"    # 对比系列：买入持有基准
_MARK_BUY = "#d03b3b"       # 买入标记（A股习惯：红买）
_MARK_SELL = "#0ca30c"      # 卖出标记（A股习惯：绿卖）
_INK = "#0b0b0b"            # 主文字
_INK_SECONDARY = "#52514e"  # 次级文字
_INK_MUTED = "#898781"      # 轴刻度等弱化文字
_GRIDLINE = "#e1e0d9"       # 网格线
_BASELINE = "#c3c2b7"       # 轴线
_SURFACE = "#fcfcfb"        # 图表背景
_FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"


# ---- 回测引擎 ----

class BacktestEngine:
    """A股回测引擎（VectorBT 封装）"""

    def __init__(
        self,
        df,
        entries,
        exits,
        *,
        init_cash: float = 1_000_000,
        commission: float = 0.00025,
        slippage: float = 0.001,
        trade_unit: int = 100,
        rf: float = 0.0,
        code: str = None,
        t_plus_1: bool = True,
        price_limit: bool = True,
        stamp_tax: bool = True,
        transfer_fee: bool = True,
        limit_defer_max: int = 5,
    ):
        """
        参数:
            df:         行情数据，需包含 open/high/low/close/volume 列和 date 列（或日期索引）
            entries:    买入信号（布尔 Series / 数组，True 表示当天买入）
            exits:      卖出信号（布尔 Series / 数组，True 表示当天卖出）
            init_cash:  初始资金（元）
            commission: 佣金费率，如 0.00025 表示万 2.5，买卖各收一次
            slippage:   滑点，如 0.001 表示成交价额外让出 0.1%
            trade_unit: 交易单位（每手股数），A 股为 100；传 None 表示不限制整手
            rf:         无风险利率（用于计算夏普比率），如 0.02 表示 2%
            code:       股票代码（涨跌停幅度按板块判断用），如 600519；不传按主板 10% 处理
            t_plus_1:   True 时买卖信号统一顺延到下一交易日执行（A 股 T+1 规则）
            price_limit: True 时涨停日买不进、跌停日卖不出，信号顺延到可成交日
            stamp_tax:  True 时卖出收取印花税 0.05%
            transfer_fee: True 时买卖双向收取过户费 0.001%
            limit_defer_max: 涨跌停导致信号无法成交时，最多顺延的天数
        """
        self._check_env()
        self.df = self._prepare_df(df)
        self.entries = self._prepare_signals(entries, "entries")
        self.exits = self._prepare_signals(exits, "exits")
        self._entries_raw = self.entries.copy()  # 原始信号（规则调整前的）
        self._exits_raw = self.exits.copy()

        # 参数校验
        if not isinstance(init_cash, (int, float)) or init_cash <= 0:
            raise ValueError(f"初始资金必须为正数：{init_cash!r}")
        if not isinstance(commission, (int, float)) or not 0 <= commission < 1:
            raise ValueError(f"佣金费率应在 [0, 1) 之间：{commission!r}")
        if not isinstance(slippage, (int, float)) or not 0 <= slippage < 1:
            raise ValueError(f"滑点应在 [0, 1) 之间：{slippage!r}")
        if trade_unit is not None and (
            not isinstance(trade_unit, (int, float)) or trade_unit <= 0
        ):
            raise ValueError(f"交易单位必须为正整数（如 100），或 None：{trade_unit!r}")
        if code is not None and not isinstance(code, str):
            raise ValueError(f"股票代码必须是字符串或 None：{code!r}")
        for name, val in [("t_plus_1", t_plus_1), ("price_limit", price_limit),
                          ("stamp_tax", stamp_tax), ("transfer_fee", transfer_fee)]:
            if not isinstance(val, bool):
                raise ValueError(f"{name} 必须是布尔值：{val!r}")
        if not isinstance(limit_defer_max, int) or limit_defer_max < 1:
            raise ValueError(f"limit_defer_max 必须为正整数：{limit_defer_max!r}")

        self.init_cash = float(init_cash)
        self.commission = float(commission)
        self.slippage = float(slippage)
        self.trade_unit = int(trade_unit) if trade_unit is not None else None
        self.rf = float(rf)
        self.code = code
        self.t_plus_1 = t_plus_1
        self.price_limit = price_limit
        self.stamp_tax = stamp_tax
        self.transfer_fee = transfer_fee
        self.limit_defer_max = limit_defer_max

        self.portfolio = None   # 回测结果（run() 之后可用）
        self._metrics = None    # 指标缓存（run() 之后可用）

    # ---- 内部工具 ----

    @staticmethod
    def _check_env():
        """确认 vectorbt 已安装"""
        if vbt is None:
            raise ImportError("未安装 vectorbt，请先运行：pip install -r requirements.txt")

    @staticmethod
    def _prepare_df(df) -> pd.DataFrame:
        """校验行情数据，并统一为按日期升序、DatetimeIndex 索引的 DataFrame"""
        if df is None or len(df) == 0:
            raise ValueError("行情数据为空")
        df = df.copy()
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")
        if not isinstance(df.index, pd.DatetimeIndex):
            raise ValueError("行情数据需要 date 列或日期索引")
        missing = [c for c in _REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(f"行情数据缺少列：{missing}")
        df = df.sort_index()
        df = df[~df.index.duplicated(keep="last")]
        if (df[["open", "high", "low", "close"]] <= 0).any().any():
            raise ValueError("价格数据存在非正值，请检查数据")
        if (df["high"] < df["low"]).any():
            raise ValueError("存在最高价低于最低价的异常行")
        return df

    def _prepare_signals(self, signals, name: str) -> pd.Series:
        """校验信号：转换为与行情数据等长、同索引的布尔 Series"""
        if signals is None:
            raise ValueError(f"{name} 信号不能为空")
        sig = pd.Series(signals).fillna(False).astype(bool)
        if len(sig) != len(self.df):
            raise ValueError(
                f"{name} 信号长度({len(sig)})与行情数据({len(self.df)})不一致"
            )
        # 始终按位置对齐到行情的精确索引，避免索引名称、频率等元数据
        # 差异导致 vectorbt 在内部广播时拒绝执行。
        return pd.Series(sig.to_numpy(), index=self.df.index)

    # ---- A股规则处理 ----

    def _apply_a_share_rules(self):
        """按 A 股规则调整信号并计算费用，返回 (entries, exits, fees)

        规则（详见 __init__ 参数说明）:
            1. T+1：信号统一顺延 1 个交易日执行
            2. 涨跌停：涨停日买不进、跌停日卖不出，信号顺延到可成交日
            3. 同日买卖冲突：保留卖出、丢弃买入（风控优先）
            4. 费用：佣金 + 过户费双向，印花税卖出时收
        """
        idx = self.df.index
        n = len(idx)
        close = self.df["close"]
        entries = self._entries_raw.copy()
        exits = self._exits_raw.copy()

        # 1. T+1：信号在收盘后产生，统一顺延到下一交易日执行
        if self.t_plus_1:
            entries = entries.shift(1).fillna(False).astype(bool)
            exits = exits.shift(1).fillna(False).astype(bool)

        # 2. 涨跌停：执行日封板则无法成交，顺延到可成交日
        if self.price_limit:
            limit_pct = get_price_limit(self.code) if self.code else 0.10
            prev_close = close.shift(1)
            limit_up = (prev_close * (1 + limit_pct)).round(2)    # 涨停价
            limit_down = (prev_close * (1 - limit_pct)).round(2)  # 跌停价

            def defer(sig, blocked):
                """把 blocked 日无法执行的信号顺延到下一个可执行日（超过上限则丢弃）"""
                out = sig.copy()
                for i in range(n):
                    if not sig.iloc[i]:
                        continue
                    placed = False
                    for j in range(i, min(i + self.limit_defer_max + 1, n)):
                        if blocked.iloc[j]:
                            continue
                        out.iloc[j] = True
                        placed = True
                        break
                    if placed and j != i:
                        out.iloc[i] = False  # 信号已顺延，清除原位置
                    elif not placed:
                        out.iloc[i] = False  # 连续多日封板：丢弃该信号
                return out

            entries = defer(entries, close >= limit_up)    # 涨停买不进
            exits = defer(exits, close <= limit_down)      # 跌停卖不出

        # 3. 同日买卖冲突：保留卖出、丢弃买入（风控优先，先离场不新建仓）
        conflict = entries & exits
        entries = entries & ~conflict

        # 4. 费用：佣金 + 过户费（双向），印花税（卖出时收）
        fee_rate = self.commission + (TRANSFER_FEE_RATE if self.transfer_fee else 0.0)
        fees = pd.Series(fee_rate, index=idx, dtype=float)
        if self.stamp_tax:
            fees = fees + STAMP_TAX_RATE * exits.astype(float)

        return entries, exits, fees

    # ---- 对外方法 ----

    @staticmethod
    def _detect_freq(idx: pd.DatetimeIndex) -> str:
        """根据数据的时间间隔推断频率：日线 "1D"，分钟线 "5min" 等
        （传给 vectorbt，保证夏普等指标按正确频率年化）"""
        if len(idx) < 2:
            return "1D"
        median_sec = float((idx[1:] - idx[:-1]).median().total_seconds())
        if median_sec < 86400:  # 日内数据（分钟线）
            return f"{max(1, int(round(median_sec / 60)))}min"
        return "1D"

    def run(self) -> "BacktestEngine":
        """执行回测，完成后可调用 get_metrics() 和 plot()"""
        self.entries, self.exits, fees = self._apply_a_share_rules()
        self.portfolio = vbt.Portfolio.from_signals(
            close=self.df["close"],
            entries=self.entries,
            exits=self.exits,
            init_cash=self.init_cash,
            fees=fees,                          # 佣金 + 过户费 + 卖出印花税（动态费用）
            slippage=self.slippage,             # 滑点
            size_type="percent",                # 按可用资金比例买入
            size=100.0,                         # 全仓
            size_granularity=self.trade_unit,   # 整手向下取整（A股 100 股/手）
            allow_partial=False,                # 买不起一手时不下单（真实 A 股规则）
            accumulate=False,                   # 持仓期间重复买入信号忽略
            freq=self._detect_freq(pd.DatetimeIndex(self.df.index)),
        )
        self._metrics = self._compute_metrics()
        return self

    def get_metrics(self) -> dict:
        """获取回测指标字典（需先 run()）

        返回:
            {
                "累计收益率": 总收益，如 0.25 表示 25%,
                "年化收益率": 按日历天数折算的年化收益,
                "最大回撤":   最大回撤（正值，如 0.235 表示回撤 23.5%）,
                "夏普比率":   风险调整后收益（rf 无风险利率参与计算）,
                "胜率":       盈利交易占比，如 0.6 表示 60%,
                "总交易次数": 完整交易次数,
                "期末总资产": 回测结束时的账户总资产（元）,
                "基准收益率": 同期买入持有的收益（用于对比策略表现）,
            }
        """
        if self._metrics is None:
            raise RuntimeError("请先调用 run() 执行回测")
        return self._metrics

    def plot(self) -> dict:
        """生成图表（需先 run()）

        返回:
            {"equity": 净值曲线图, "position": 持仓变化图}，均为 plotly Figure，
            可直接传给 st.plotly_chart() 展示，或调用 .show() 查看。
        """
        if self.portfolio is None:
            raise RuntimeError("请先调用 run() 执行回测")
        return {"equity": self._plot_equity(), "position": self._plot_position()}

    def get_portfolio(self):
        """返回 vectorbt 原始回测对象（高级用法，需先 run()）"""
        if self.portfolio is None:
            raise RuntimeError("请先调用 run() 执行回测")
        return self.portfolio

    # ---- 指标计算 ----

    def _compute_metrics(self) -> dict:
        """从 vectorbt 统计结果中提取指标"""
        try:
            # vectorbt 1.x 通过 metric_settings 给指标传参数（如夏普比率的无风险利率）
            stats = self.portfolio.stats(
                metric_settings=dict(sharpe_ratio=dict(risk_free=self.rf))
            )
        except Exception:
            stats = self.portfolio.stats()

        def stat(key, default=np.nan):
            return stats.get(key, default)

        total_return = float(stat("Total Return [%]") / 100)

        # 年化收益率：vectorbt 1.x 默认指标里没有，按日历天数自己折算
        n_days = (self.df.index[-1] - self.df.index[0]).days
        if total_return > -1 and n_days > 0:
            annual_return = (1 + total_return) ** (365.25 / n_days) - 1
        else:
            annual_return = -1.0 if total_return <= -1 else np.nan

        # 胜率 / 交易次数：按已平仓交易记录统计（与风控分析的 RiskAnalyzer 口径一致）
        records = self.portfolio.trades.records_readable
        if "Status" in records.columns:
            closed = records[records["Status"] == "Closed"]
            n_trades = len(closed)
            win_rate = float((closed["PnL"] > 0).mean()) if n_trades else 0.0
        else:
            n_trades = int(stat("Total Trades", 0))
            win_rate = float(stat("Win Rate [%]") / 100)

        metrics = {
            "累计收益率": total_return,
            "年化收益率": float(annual_return),
            "最大回撤": float(abs(stat("Max Drawdown [%]") / 100)),  # 正值
            "夏普比率": float(stat("Sharpe Ratio")),
            "胜率": win_rate,
            "总交易次数": n_trades,
            "总手续费": float(stat("Total Fees Paid", 0)),
            "期末总资产": float(self.portfolio.value().iloc[-1]),
            "基准收益率": float(stat("Benchmark Return [%]") / 100),  # 买入持有对比
        }
        return metrics

    # ---- 图表 ----

    def _plot_equity(self):
        """净值曲线：策略 vs 买入持有基准"""
        equity = self.portfolio.value()
        benchmark = self.df["close"] / self.df["close"].iloc[0] * self.init_cash

        fig = go.Figure()
        fig.add_scatter(
            x=equity.index, y=equity.values, name="策略净值",
            mode="lines", line=dict(color=_LINE_BLUE, width=2),
            hovertemplate="策略净值 %{y:,.0f} 元<extra></extra>",
        )
        fig.add_scatter(
            x=benchmark.index, y=benchmark.values, name="买入持有",
            mode="lines", line=dict(color=_LINE_ORANGE, width=2, dash="dot"),
            hovertemplate="买入持有 %{y:,.0f} 元<extra></extra>",
        )
        self._style_layout(fig, title="净值曲线：策略 vs 买入持有", y_title="账户资产（元）")
        return fig

    def _plot_position(self):
        """持仓变化：持仓股数 + 买卖点标记"""
        position = self.portfolio.position_mask()  # 每天的持仓股数

        fig = go.Figure()
        fig.add_scatter(
            x=position.index, y=position.values, name="持仓股数",
            mode="lines", line=dict(color=_LINE_BLUE, width=2, shape="hv"),
            fill="tozeroy", fillcolor="rgba(42,120,214,0.08)",
            hovertemplate="持仓 %{y:,.0f} 股<extra></extra>",
        )

        entry_dates = self.df.index[self.entries]
        exit_dates = self.df.index[self.exits]
        if len(entry_dates):
            fig.add_scatter(
                x=entry_dates, y=position.loc[entry_dates].values, name="买入",
                mode="markers",
                marker=dict(symbol="triangle-up", color=_MARK_BUY, size=9,
                            line=dict(color=_SURFACE, width=1)),
                hovertemplate="买入，持仓 %{y:,.0f} 股<extra></extra>",
            )
        if len(exit_dates):
            fig.add_scatter(
                x=exit_dates, y=position.loc[exit_dates].values, name="卖出",
                mode="markers",
                marker=dict(symbol="triangle-down", color=_MARK_SELL, size=9,
                            line=dict(color=_SURFACE, width=1)),
                hovertemplate="卖出，持仓 %{y:,.0f} 股<extra></extra>",
            )
        self._style_layout(fig, title="持仓变化（▲买入 / ▼卖出）", y_title="持仓股数")
        return fig

    @staticmethod
    def _style_layout(fig, title: str, y_title: str):
        """统一图表样式：细线条、弱化网格、浅色背景"""
        fig.update_layout(
            title=dict(text=title, font=dict(color=_INK, size=16, family=_FONT)),
            xaxis=dict(
                showgrid=True, gridcolor=_GRIDLINE, zeroline=False,
                linecolor=_BASELINE,
                tickfont=dict(color=_INK_MUTED, family=_FONT, size=11),
                title=dict(text="日期", font=dict(color=_INK_SECONDARY, family=_FONT)),
            ),
            yaxis=dict(
                showgrid=True, gridcolor=_GRIDLINE, zeroline=False,
                linecolor=_BASELINE,
                tickfont=dict(color=_INK_MUTED, family=_FONT, size=11),
                title=dict(text=y_title, font=dict(color=_INK_SECONDARY, family=_FONT)),
            ),
            hovermode="x unified",
            plot_bgcolor=_SURFACE,
            paper_bgcolor=_SURFACE,
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02, x=0,
                font=dict(color=_INK_SECONDARY, family=_FONT, size=11),
            ),
            margin=dict(l=10, r=10, t=50, b=10),
        )


# ---- 测试用例 ----

def run_test():
    """测试用例：用双均线策略回测贵州茅台(600519)近 3 年数据

    运行方式：
        python core/backtest_engine.py
    """
    import datetime
    import os
    import sys

    # 保证能从项目根目录导入其他模块
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from utils.data_loader import load_daily_data
    from strategies.moving_average import MovingAverageStrategy

    print("===== 测试 1：双均线策略回测（贵州茅台 近3年）=====")
    print()
    end = datetime.date.today()
    start = end - datetime.timedelta(days=365 * 3)
    df = load_daily_data("600519", start, end)

    strategy = MovingAverageStrategy(fast=5, slow=20)
    entries, exits = strategy.generate_signals(df)

    bt = BacktestEngine(df, entries, exits, init_cash=1_000_000)
    bt.run()

    metrics = bt.get_metrics()
    print("回测指标：")
    for key, value in metrics.items():
        if "收益率" in key or "回撤" in key or "胜率" in key:
            print(f"  {key}: {value:.2%}")
        elif "总资产" in key:
            print(f"  {key}: {value:,.0f} 元")
        else:
            print(f"  {key}: {value}")

    # ---- 指标合理性断言 ----
    assert metrics["总交易次数"] > 0, "双均线策略近3年应有交易"
    assert -1 < metrics["累计收益率"] < 10, f"累计收益率异常：{metrics['累计收益率']}"
    assert 0 <= metrics["胜率"] <= 1, f"胜率异常：{metrics['胜率']}"
    assert metrics["最大回撤"] >= 0, f"最大回撤异常：{metrics['最大回撤']}"
    assert metrics["期末总资产"] > 0, "期末总资产应大于 0"
    print("✓ 指标合理性校验通过")
    print()

    # ---- 图表生成断言 ----
    figs = bt.plot()
    assert set(figs.keys()) == {"equity", "position"}, "图表应包含 equity 和 position"
    assert len(figs["equity"].data) == 2, "净值曲线应有 2 条线（策略 + 基准）"
    assert len(figs["position"].data) >= 1, "持仓图至少应有持仓线"
    print("✓ 图表生成通过（净值曲线 2 条线，持仓图含买卖标记）")
    print()

    # ---- 测试 2：正确性验证（关闭A股规则、无费用滑点、无整手限制 => 全仓买入持有）----
    print("===== 测试 2：正确性验证（零费用零滑点 = 理论买入持有收益）=====")
    entries_hold = pd.Series(False, index=df.index)
    entries_hold.iloc[0] = True
    exits_hold = pd.Series(False, index=df.index)
    exits_hold.iloc[-1] = True

    bt_hold = BacktestEngine(
        df, entries_hold, exits_hold,
        init_cash=1_000_000, commission=0.0, slippage=0.0, trade_unit=None,
        t_plus_1=False, price_limit=False, stamp_tax=False, transfer_fee=False,
    )
    bt_hold.run()
    actual = bt_hold.get_metrics()["累计收益率"]
    expected = df["close"].iloc[-1] / df["close"].iloc[0] - 1
    assert abs(actual - expected) < 1e-6, f"回测逻辑有误：{actual:.6f} != {expected:.6f}"

    equity_final = bt_hold.get_portfolio().value().iloc[-1]
    assert abs(equity_final - 1_000_000 * (1 + expected)) < 1, "期末资产与收益率不一致"
    print(f"✓ 正确性验证通过：回测收益 {actual:.6%} == 理论买入持有 {expected:.6%}")
    print()

    # ---- 测试 3：T+1（信号次日执行）----
    print("===== 测试 3：T+1 规则（信号次日执行）=====")
    idx3 = pd.date_range("2024-01-01", periods=10, freq="D")
    flat = pd.DataFrame({
        "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 10000,
    }, index=idx3)
    e3 = pd.Series(False, index=idx3); e3.iloc[2] = True   # 第 2 天产生买入信号
    x3 = pd.Series(False, index=idx3); x3.iloc[4] = True   # 第 4 天产生卖出信号

    bt3 = BacktestEngine(flat, e3, x3, t_plus_1=True, price_limit=False,
                         stamp_tax=False, transfer_fee=False).run()
    mask3 = bt3.get_portfolio().position_mask()
    assert mask3.iloc[2] == 0, "信号当天不应持仓（次日执行）"
    assert mask3.iloc[3] == 1, "买入信号应顺延到第 3 天执行"
    assert mask3.iloc[4] == 1, "持仓中"
    assert mask3.iloc[5] == 0, "卖出信号应顺延到第 5 天执行"

    # 同日买卖冲突：保留卖出、丢弃买入 → 空仓时不成交
    x3b = pd.Series(False, index=idx3); x3b.iloc[2] = True  # 与买入同一天
    bt3b = BacktestEngine(flat, e3, x3b, t_plus_1=True, price_limit=False,
                          stamp_tax=False, transfer_fee=False).run()
    assert (bt3b.get_portfolio().position_mask() == 0).all(), "同日买卖冲突应不成交"
    print("✓ T+1：信号次日执行、同日冲突风控优先（保留卖出丢弃买入）")
    print()

    # ---- 测试 4：涨跌停（封板日无法成交，顺延到可成交日）----
    print("===== 测试 4：涨跌停规则（封板顺延）=====")
    idx4 = pd.date_range("2024-01-01", periods=15, freq="D")
    close4 = pd.Series([10.0] * 15, index=idx4)
    close4.iloc[5] = 11.0   # 第 5 天涨停（10 * 1.1 = 11.00）
    close4.iloc[11] = 9.0   # 第 11 天跌停（10 * 0.9 = 9.00）
    df4 = pd.DataFrame({
        "open": close4, "high": close4, "low": close4, "close": close4, "volume": 10000,
    }, index=idx4)
    e4 = pd.Series(False, index=idx4); e4.iloc[4] = True   # 买入信号 → 次日(第5天)涨停
    x4 = pd.Series(False, index=idx4); x4.iloc[10] = True  # 卖出信号 → 次日(第11天)跌停

    bt4 = BacktestEngine(df4, e4, x4, code="600519",
                         t_plus_1=True, price_limit=True,
                         stamp_tax=False, transfer_fee=False).run()
    mask4 = bt4.get_portfolio().position_mask()
    assert mask4.iloc[5] == 0, "涨停日应买不进（信号顺延）"
    assert mask4.iloc[6] == 1, "买入应顺延到第 6 天成交"
    assert mask4.iloc[11] == 1, "跌停日应卖不出（仍持仓）"
    assert mask4.iloc[12] == 0, "卖出应顺延到第 12 天成交"

    # 对照组：关闭涨跌停规则时，第 5 天直接买入
    bt4b = BacktestEngine(df4, e4, x4, code="600519",
                          t_plus_1=True, price_limit=False,
                          stamp_tax=False, transfer_fee=False).run()
    assert bt4b.get_portfolio().position_mask().iloc[5] == 1, "关闭规则时涨停日应能买入"
    print("✓ 涨跌停：涨停买不进顺延买入、跌停卖不出顺延卖出（对照组成立）")
    print()

    # ---- 测试 5：印花税 + 过户费（卖出费用高于买入）----
    print("===== 测试 5：印花税 + 过户费 =====")
    e5 = pd.Series(False, index=df.index); e5.iloc[100] = True
    x5 = pd.Series(False, index=df.index); x5.iloc[200] = True
    bt5_on = BacktestEngine(df, e5, x5, code="600519", init_cash=1_000_000,
                            commission=0.0, slippage=0.0).run()          # 仅印花税+过户费
    bt5_off = BacktestEngine(df, e5, x5, code="600519", init_cash=1_000_000,
                             commission=0.0, slippage=0.0,
                             stamp_tax=False, transfer_fee=False).run()  # 零费用
    fees_on = bt5_on.get_metrics()["总手续费"]
    assert fees_on > 0, "启用规则后应产生费用"
    assert bt5_off.get_metrics()["总手续费"] == 0, "关闭规则后费用应为 0"
    assert abs(bt5_on.get_metrics()["累计收益率"] - bt5_off.get_metrics()["累计收益率"]) > 0, \
        "费用应降低收益"
    print(f"✓ 印花税+过户费：单次往返产生费用 {fees_on:,.2f} 元，收益相应降低")
    print()
    print("===== 全部测试通过 =====")
    return bt, figs


if __name__ == "__main__":
    run_test()
