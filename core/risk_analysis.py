"""风控与绩效分析模块

输入回测的净值曲线和交易记录，提供：
1. 完整绩效指标：累计收益、年化收益、年化波动、夏普比率、卡玛比率、最大回撤、胜率、盈亏比
2. 回撤序列、月度收益统计、年度收益统计
3. 基础风控校验：单笔仓位限制、单日最大亏损限制、止损止盈计算
4. 格式化绩效报告：可打印（report()）、可导出为字典（to_dict()）

用法:
    from core.risk_analysis import RiskAnalyzer, analyze_portfolio

    analyzer = analyze_portfolio(engine.portfolio)   # 从回测结果直接分析
    print(analyzer.report())
    data = analyzer.to_dict()
"""

import math
import os
import sys

# 保证直接运行本文件（python core/risk_analysis.py）时能找到 core / utils 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

TRADING_DAYS = 252      # 一年交易日数（年化波动、夏普用）
DAYS_PER_YEAR = 365.25  # 一年自然日数（年化收益用，与回测引擎口径一致）


# ---------------------------------------------------------------------------
# 止损止盈计算
# ---------------------------------------------------------------------------

def stop_loss_take_profit(entry_price, stop_loss_pct=None, take_profit_pct=None):
    """按百分比计算止损价 / 止盈价

    参数:
        entry_price:     买入价（> 0）
        stop_loss_pct:   止损幅度，单位 %（如 5 表示跌 5% 止损），可选
        take_profit_pct: 止盈幅度，单位 %（如 10 表示涨 10% 止盈），可选

    返回:
        {"entry_price": 买入价, "stop_loss_price": 止损价或 None,
         "take_profit_price": 止盈价或 None}
    """
    if not isinstance(entry_price, (int, float)) or entry_price <= 0:
        raise ValueError(f"entry_price 必须为正数：{entry_price!r}")
    if stop_loss_pct is None and take_profit_pct is None:
        raise ValueError("stop_loss_pct 和 take_profit_pct 至少要填一个")
    for name, val in (("stop_loss_pct", stop_loss_pct), ("take_profit_pct", take_profit_pct)):
        if val is not None and (not isinstance(val, (int, float)) or not 0 < val < 100):
            raise ValueError(f"{name} 必须在 (0, 100) 之间：{val!r}")

    return {
        "entry_price": float(entry_price),
        # 价格四舍五入到 4 位小数，避免二进制浮点误差（如 100*0.95=94.999...）
        "stop_loss_price": round(entry_price * (1 - stop_loss_pct / 100), 4) if stop_loss_pct else None,
        "take_profit_price": round(entry_price * (1 + take_profit_pct / 100), 4) if take_profit_pct else None,
    }


def stop_loss_atr(df, period: int = 14, multiple: float = 2.0):
    """按 ATR（平均真实波幅）计算动态止损价

    止损价 = 最新收盘价 - multiple × ATR

    参数:
        df:       行情数据，含 high/low/close 列
        period:   ATR 计算周期（天），默认 14
        multiple: ATR 倍数，默认 2.0

    返回:
        {"stop_loss_price": 止损价, "atr": 最新 ATR 值}
    """
    if not isinstance(period, int) or period < 2:
        raise ValueError(f"period 必须为不小于 2 的整数：{period!r}")
    if not isinstance(multiple, (int, float)) or multiple <= 0:
        raise ValueError(f"multiple 必须为正数：{multiple!r}")

    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"],
         (df["high"] - prev_close).abs(),
         (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(period).mean()
    last_atr = atr.iloc[-1]
    if pd.isna(last_atr):
        raise ValueError("数据不足，无法计算 ATR")
    return {
        "stop_loss_price": float(df["close"].iloc[-1] - multiple * last_atr),
        "atr": float(last_atr),
    }


# ---------------------------------------------------------------------------
# 绩效分析器
# ---------------------------------------------------------------------------

class RiskAnalyzer:
    """风控与绩效分析器

    参数:
        equity:         净值曲线（pd.Series，索引必须是日期 DatetimeIndex）
        trades:         交易记录（可选，胜率/盈亏比需要），支持三种形式：
                        1. vectorbt 的 trades 对象（自动读取 records_readable）
                        2. 含 PnL 列的 DataFrame
                        3. 每笔盈亏的序列
        position_value: 每日持仓市值（pd.Series，索引为日期），
                        做仓位限制校验时需要
        rf:             无风险利率（年化小数，如 0.02 表示 2%），默认 0.0
    """

    def __init__(self, equity, trades=None, position_value=None, rf: float = 0.0):
        equity = pd.Series(equity).dropna()
        if not isinstance(equity.index, pd.DatetimeIndex):
            raise ValueError("equity 的索引必须是日期（DatetimeIndex）")
        if len(equity) < 2:
            raise ValueError("净值数据至少需要 2 个点")
        self.equity = equity.sort_index()
        self.trades_pnl = self._normalize_trades(trades)
        self.position_value = None
        if position_value is not None:
            self.position_value = pd.Series(position_value).reindex(self.equity.index).fillna(0.0)
        self.rf = float(rf)

    @staticmethod
    def _normalize_trades(trades):
        """把各种形式的交易记录统一成每笔盈亏的 Series"""
        if trades is None:
            return None
        if hasattr(trades, "records_readable"):
            rec = trades.records_readable
            # 只统计已平仓的交易（Status == "Closed"），与引擎的胜率口径一致
            if "Status" in rec.columns:
                rec = rec[rec["Status"] == "Closed"]
            col = "PnL" if "PnL" in rec.columns else "pnl"
            return pd.Series(rec[col].astype(float).values)
        if isinstance(trades, pd.DataFrame):
            for col in ("PnL", "pnl", "盈亏"):
                if col in trades.columns:
                    return pd.Series(trades[col].astype(float).values)
            raise ValueError("trades DataFrame 中找不到 PnL 列")
        return pd.Series(trades, dtype=float)

    # -- 基础序列 -----------------------------------------------------------

    def daily_returns(self):
        """日收益率序列"""
        return self.equity.pct_change().dropna()

    def drawdown_series(self):
        """回撤序列：当日净值相对历史最高净值的跌幅

        负值表示回撤，0 表示创历史新高。可直接用于画回撤曲线图。
        """
        return self.equity / self.equity.cummax() - 1

    def monthly_returns(self):
        """月度收益率序列（每月最后一个交易日的净值环比），索引为月末日期"""
        month_end = self.equity.resample("ME").last()
        return month_end.pct_change().dropna()

    def yearly_returns(self):
        """年度收益率序列（每年最后一个交易日的净值环比），索引为年末日期"""
        year_end = self.equity.resample("YE").last()
        return year_end.pct_change().dropna()

    # -- 绩效指标 -----------------------------------------------------------

    def compute_metrics(self):
        """计算完整绩效指标，返回指标字典

        指标:
            累计收益率 / 年化收益率 / 年化波动率 / 夏普比率 /
            卡玛比率 / 最大回撤 / 胜率 / 盈亏比 / 总交易次数
        """
        equity = self.equity
        daily = self.daily_returns()

        total_return = equity.iloc[-1] / equity.iloc[0] - 1

        n_days = (equity.index[-1] - equity.index[0]).days
        annual_return = (1 + total_return) ** (DAYS_PER_YEAR / n_days) - 1 if n_days > 0 else 0.0

        # 每年周期数：日线 = 252；分钟线按“日均根数 × 252”折算，
        # 保证波动率/夏普的年化因子与数据频率匹配（日线结果与原来完全一致）
        idx = pd.DatetimeIndex(equity.index)
        if len(idx) >= 2 and float((idx[1:] - idx[:-1]).median().total_seconds()) < 86400:
            bars_per_day = len(idx) / max(idx.normalize().nunique(), 1)
            ppy = max(TRADING_DAYS * bars_per_day, TRADING_DAYS)
        else:
            ppy = TRADING_DAYS

        daily_std = daily.std(ddof=1)
        volatility = daily_std * math.sqrt(ppy)
        sharpe = (daily.mean() - self.rf / ppy) / daily_std * math.sqrt(ppy) \
            if daily_std > 0 else 0.0

        max_dd = abs(self.drawdown_series().min())
        calmar = annual_return / max_dd if max_dd > 1e-12 else 0.0

        # 胜率 / 盈亏比（需要交易记录）
        if self.trades_pnl is None or len(self.trades_pnl) == 0:
            win_rate, profit_loss_ratio, n_trades = None, None, 0
        else:
            pnl = self.trades_pnl
            n_trades = len(pnl)
            wins = (pnl > 0).sum()
            win_rate = wins / n_trades
            if wins > 0:
                if (pnl < 0).sum() > 0:
                    profit_loss_ratio = float(pnl[pnl > 0].mean() / (-pnl[pnl < 0].mean()))
                else:
                    profit_loss_ratio = float("inf")  # 没有亏损单
            else:
                profit_loss_ratio = 0.0

        return {
            "累计收益率": float(total_return),
            "年化收益率": float(annual_return),
            "年化波动率": float(volatility),
            "夏普比率": float(sharpe),
            "卡玛比率": float(calmar),
            "最大回撤": float(max_dd),
            "胜率": float(win_rate) if win_rate is not None else None,
            "盈亏比": float(profit_loss_ratio) if profit_loss_ratio is not None else None,
            "总交易次数": int(n_trades),
        }

    # -- 风控校验 -----------------------------------------------------------

    def check_position_limit(self, max_pct):
        """单笔仓位限制校验：持仓市值占当日总资产的比例不得超过 max_pct%

        参数:
            max_pct: 仓位上限，单位 %（如 80 表示单票仓位不得超过总资产的 80%）

        返回:
            违规记录列表 [{"date": "2024-03-15", "position_pct": 87.3}, ...]
        """
        if not isinstance(max_pct, (int, float)) or not 0 < max_pct <= 100:
            raise ValueError(f"max_pct 必须在 (0, 100] 之间：{max_pct!r}")
        if self.position_value is None:
            raise ValueError(
                "仓位限制校验需要 position_value（每日持仓市值），"
                "请在构造 RiskAnalyzer 时传入，或用 analyze_portfolio() 自动提取"
            )
        ratio = self.position_value / self.equity
        bad = ratio[ratio > max_pct / 100]
        return [
            {"date": str(d.date()), "position_pct": round(float(r) * 100, 2)}
            for d, r in bad.items()
        ]

    def check_daily_loss(self, max_loss_pct):
        """单日最大亏损限制校验：单日跌幅（相对前一日净值）不得超过 max_loss_pct%

        参数:
            max_loss_pct: 单日亏损上限，单位 %（如 3 表示单日最多允许亏 3%）

        返回:
            违规记录列表 [{"date": "2024-03-15", "loss_pct": 4.2}, ...]
        """
        if not isinstance(max_loss_pct, (int, float)) or not 0 < max_loss_pct <= 100:
            raise ValueError(f"max_loss_pct 必须在 (0, 100] 之间：{max_loss_pct!r}")
        daily = self.daily_returns()
        bad = daily[daily <= -max_loss_pct / 100]
        return [
            {"date": str(d.date()), "loss_pct": round(float(r) * 100, 2)}
            for d, r in bad.items()
        ]

    # -- 报告与导出 -----------------------------------------------------------

    @staticmethod
    def _fmt_pct(x):
        """小数 -> "12.34%"；None/NaN -> "—"；inf -> "∞" """
        if x is None:
            return "—"
        if isinstance(x, float) and math.isnan(x):
            return "—"
        if isinstance(x, float) and math.isinf(x):
            return "∞"
        return f"{x * 100:.2f}%"

    @staticmethod
    def _fmt_num(x):
        """数字 -> "1.2345"；None/NaN -> "—"；inf -> "∞" """
        if x is None:
            return "—"
        if isinstance(x, float) and math.isnan(x):
            return "—"
        if isinstance(x, float) and math.isinf(x):
            return "∞"
        return f"{x:.4f}"

    def _risk_check_lines(self, risk_limits, max_show: int = 10):
        """生成风控校验结果文字，返回 (行列表, 校验结果字典)"""
        if not risk_limits:
            return ["  未设置风控限额，跳过校验",
                    "  可在 report(risk_limits={\"position_limit_pct\": 80, \"daily_loss_limit_pct\": 3}) 中传入"], {}

        results = {}
        lines = []
        if "position_limit_pct" in risk_limits:
            limit = risk_limits["position_limit_pct"]
            try:
                bad = self.check_position_limit(limit)
            except ValueError as e:
                lines.append(f"  仓位限制 {limit}% : 无法校验（{e}）")
            else:
                results["position_limit_violations"] = bad
                if bad:
                    lines.append(f"  仓位限制 {limit}% : 超标 {len(bad)} 天")
                    for v in bad[:max_show]:
                        lines.append(f"    {v['date']}  仓位 {v['position_pct']}%")
                    if len(bad) > max_show:
                        lines.append(f"    …… 其余 {len(bad) - max_show} 天略")
                else:
                    lines.append(f"  仓位限制 {limit}% : 未超标 ✓")

        if "daily_loss_limit_pct" in risk_limits:
            limit = risk_limits["daily_loss_limit_pct"]
            bad = self.check_daily_loss(limit)
            results["daily_loss_violations"] = bad
            if bad:
                lines.append(f"  单日亏损限制 {limit}% : 超标 {len(bad)} 天")
                for v in bad[:max_show]:
                    lines.append(f"    {v['date']}  单日跌幅 {v['loss_pct']}%")
                if len(bad) > max_show:
                    lines.append(f"    …… 其余 {len(bad) - max_show} 天略")
            else:
                lines.append(f"  单日亏损限制 {limit}% : 未超标 ✓")

        return lines, results

    def report(self, risk_limits=None) -> str:
        """生成格式化的绩效报告（字符串），可直接 print

        参数:
            risk_limits: 风控限额（可选），如
                {"position_limit_pct": 80, "daily_loss_limit_pct": 3}

        返回:
            多行文本报告
        """
        m = self.compute_metrics()
        lines = ["=" * 56, "绩效分析报告", "=" * 56]

        lines.append("【一、绩效指标】")
        lines.append(f"  累计收益率    : {self._fmt_pct(m['累计收益率'])}")
        lines.append(f"  年化收益率    : {self._fmt_pct(m['年化收益率'])}")
        lines.append(f"  总交易次数    : {m['总交易次数']}")

        lines.append("【二、风险指标】")
        lines.append(f"  年化波动率    : {self._fmt_pct(m['年化波动率'])}")
        lines.append(f"  夏普比率      : {self._fmt_num(m['夏普比率'])}")
        lines.append(f"  卡玛比率      : {self._fmt_num(m['卡玛比率'])}")
        lines.append(f"  最大回撤      : {self._fmt_pct(m['最大回撤'])}")
        lines.append(f"  胜率          : {self._fmt_pct(m['胜率'])}")
        lines.append(f"  盈亏比        : {self._fmt_num(m['盈亏比'])}")

        lines.append("【三、月度收益（%）】")
        monthly = self.monthly_returns()
        if monthly.empty:
            lines.append("  数据不足一个月")
        else:
            yearly = self.yearly_returns()
            year_map = dict(zip(yearly.index.year, yearly))
            years = sorted(monthly.index.year.unique())
            lines.append("  年\\月  | " + " ".join(f"{mm:>6}" for mm in range(1, 13)) + " |   全年")
            for y in years:
                cells = []
                for mm in range(1, 13):
                    sub = monthly[(monthly.index.year == y) & (monthly.index.month == mm)]
                    cells.append(f"{sub.iloc[0] * 100:>6.2f}" if len(sub) else "     —")
                full = year_map.get(y)
                cells.append(f"{full * 100:>6.2f}" if full is not None else "     —")
                lines.append(f"  {y} | " + "  ".join(cells))

        lines.append("【四、年度收益（%）】")
        yearly = self.yearly_returns()
        if yearly.empty:
            lines.append("  数据不足一年")
        else:
            for d, v in yearly.items():
                lines.append(f"  {d.year} : {self._fmt_pct(v)}")

        lines.append("【五、风控校验】")
        risk_lines, _ = self._risk_check_lines(risk_limits)
        lines.extend(risk_lines)

        return "\n".join(lines)

    def to_dict(self, risk_limits=None) -> dict:
        """导出为字典（可保存为 JSON）

        参数:
            risk_limits: 同 report()

        返回:
            {"metrics": {...}, "monthly_returns": {"2024-01": 0.02, ...},
             "yearly_returns": {"2024": 0.15, ...}, "risk_checks": {...}}
            胜率无交易记录时为 None，盈亏比无亏损单时为 None
        """

        def safe(x):
            """转成 float；None / NaN / inf 一律转 None，保证可序列化"""
            if x is None:
                return None
            x = float(x)
            return x if math.isfinite(x) else None

        metrics = {
            k: (int(v) if k == "总交易次数" else safe(v))
            for k, v in self.compute_metrics().items()
        }
        monthly = {f"{d.year}-{d.month:02d}": safe(v) for d, v in self.monthly_returns().items()}
        yearly = {str(d.year): safe(v) for d, v in self.yearly_returns().items()}

        _, risk_checks = self._risk_check_lines(risk_limits)
        return {
            "metrics": metrics,
            "monthly_returns": monthly,
            "yearly_returns": yearly,
            "risk_checks": risk_checks,
        }


def analyze_portfolio(portfolio, rf: float = 0.0) -> RiskAnalyzer:
    """从 vectorbt 的 portfolio 对象直接创建分析器

    参数:
        portfolio: 回测引擎的 portfolio（engine.run() 之后用 engine.portfolio）
        rf:        无风险利率（年化小数），默认 0.0

    返回:
        RiskAnalyzer 实例
    """
    return RiskAnalyzer(
        equity=portfolio.value(),
        trades=portfolio.trades,
        position_value=portfolio.asset_value(),  # 每日持仓市值
        rf=rf,
    )


def trades_table(portfolio) -> pd.DataFrame:
    """vectorbt 交易记录转中文列名明细表（Web 界面与批量回测共用）

    参数:
        portfolio: vectorbt 的 portfolio 对象（engine.run() 之后用 engine.portfolio）

    返回:
        DataFrame，列：买入日期 / 卖出日期 / 买入价 / 卖出价 /
                     数量（股） / 盈亏（元） / 收益率 / 状态
    """
    rec = portfolio.trades.records_readable
    status_map = {"Closed": "已平仓", "Open": "持仓中"}
    return pd.DataFrame({
        "买入日期": [str(t.date()) for t in rec["Entry Timestamp"]],
        "卖出日期": [str(t.date()) if pd.notna(t) else "—" for t in rec["Exit Timestamp"]],
        "买入价": rec["Avg Entry Price"].round(2),
        "卖出价": rec["Avg Exit Price"].round(2),
        "数量（股）": rec["Size"].astype(int),
        "盈亏（元）": rec["PnL"].round(2),
        "收益率": rec["Return"],
        "状态": rec["Status"].map(status_map).fillna("—"),
    })


def run_test():
    """测试：验证指标与引擎结果一致、统计与风控逻辑正确

    运行方式（在项目根目录下）：
        python core/risk_analysis.py
    """
    import datetime
    import json

    from core.backtest_engine import BacktestEngine
    from strategies.double_ma import DoubleMAStrategy
    from utils.data_loader import load_daily_data

    print("===== 风控绩效分析测试开始 =====")
    end = datetime.date.today()
    start = end - datetime.timedelta(days=3 * 365)
    df = load_daily_data("600519", start, end)
    entries, exits = DoubleMAStrategy().generate_signals(df)
    engine = BacktestEngine(df, entries, exits).run()
    engine_metrics = engine.get_metrics()

    # 1. 与回测引擎的指标一致
    analyzer = analyze_portfolio(engine.portfolio)
    m = analyzer.compute_metrics()
    assert abs(m["累计收益率"] - engine_metrics["累计收益率"]) < 1e-9, "累计收益率与引擎不一致"
    assert abs(m["最大回撤"] - engine_metrics["最大回撤"]) < 1e-9, "最大回撤与引擎不一致"
    assert abs(m["胜率"] - engine_metrics["胜率"]) < 1e-9, "胜率与引擎不一致"
    assert m["总交易次数"] == engine_metrics["总交易次数"], "交易次数与引擎不一致"
    assert 0 <= m["最大回撤"] <= 1 and m["年化波动率"] > 0
    print(f"  ✓ 与引擎指标一致：累计 {m['累计收益率']:.2%}，最大回撤 {m['最大回撤']:.2%}，"
          f"胜率 {m['胜率']:.2%}，交易 {m['总交易次数']} 次")
    print(f"  ✓ 全指标：年化 {m['年化收益率']:.2%}，波动 {m['年化波动率']:.2%}，"
          f"夏普 {m['夏普比率']:.4f}，卡玛 {m['卡玛比率']:.4f}，盈亏比 {m['盈亏比']:.4f}")

    # 2. 回撤序列
    dd = analyzer.drawdown_series()
    assert (dd <= 1e-9).all(), "回撤序列不应出现正值"
    assert abs(dd.min() + m["最大回撤"]) < 1e-9, "回撤序列最小值应与最大回撤一致"
    print(f"  ✓ 回撤序列：最大回撤 {dd.min():.2%}，出现在 {dd.idxmin().date()}")

    # 3. 月度/年度收益：复利相乘应还原区间收益
    monthly = analyzer.monthly_returns()
    assert len(monthly) >= 24, "近 3 年应有至少 24 个月度收益"
    month_end = analyzer.equity.resample("ME").last()
    expected = month_end.iloc[-1] / month_end.iloc[0] - 1
    assert abs((1 + monthly).prod() - 1 - expected) < 1e-9, "月度收益复利不一致"
    yearly = analyzer.yearly_returns()
    year_end = analyzer.equity.resample("YE").last()
    expected_y = year_end.iloc[-1] / year_end.iloc[0] - 1
    assert abs((1 + yearly).prod() - 1 - expected_y) < 1e-9, "年度收益复利不一致"
    print(f"  ✓ 月度 {len(monthly)} 期 / 年度 {len(yearly)} 期，复利还原一致")

    # 4. 仓位限制校验（构造 90% 持仓的 10 天超限场景）
    pv = analyzer.equity * 0.5
    pv.iloc[100:110] = analyzer.equity.iloc[100:110] * 0.9
    checker = RiskAnalyzer(analyzer.equity, position_value=pv)
    bad = checker.check_position_limit(80)
    assert len(bad) == 10, f"应查出 10 天超限，实际 {len(bad)}"
    assert all(89 < v["position_pct"] <= 90 for v in bad)
    assert checker.check_position_limit(100) == [], "100% 限额不应有超限"
    print(f"  ✓ 仓位限制校验：80% 限额查出 {len(bad)} 天超限（90% 持仓）")

    # 5. 单日亏损校验
    daily = analyzer.daily_returns()
    worst = daily.min()
    limit = -worst * 0.9  # 取最差单日跌幅的 90% 作限额，保证至少 1 天超限
    bad = analyzer.check_daily_loss(limit * 100)
    assert len(bad) >= 1
    assert analyzer.check_daily_loss(100) == [], "100% 亏损限额不应有超限"
    print(f"  ✓ 单日亏损校验：{limit * 100:.2f}% 限额查出 {len(bad)} 天超限（最差单日 {worst:.2%}）")

    # 6. 止损止盈计算
    s = stop_loss_take_profit(100, stop_loss_pct=5, take_profit_pct=10)
    assert s == {"entry_price": 100.0, "stop_loss_price": 95.0, "take_profit_price": 110.0}
    s2 = stop_loss_take_profit(100, stop_loss_pct=8)
    assert s2["take_profit_price"] is None and abs(s2["stop_loss_price"] - 92.0) < 1e-9
    for bad_kwargs in [{"stop_loss_pct": 150}, {"stop_loss_pct": -5}, {}]:
        try:
            stop_loss_take_profit(100, **bad_kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad_kwargs} 应该抛出 ValueError")
    atr = stop_loss_atr(df)
    assert atr["atr"] > 0 and atr["stop_loss_price"] < df["close"].iloc[-1]
    print(f"  ✓ 止损止盈：5%止损→95 / 10%止盈→110；ATR 止损价 {atr['stop_loss_price']:.2f}（ATR {atr['atr']:.2f}）")

    # 7. 报告与导出
    report = analyzer.report(risk_limits={"position_limit_pct": 80, "daily_loss_limit_pct": 5})
    for key in ["绩效分析报告", "绩效指标", "风险指标", "月度收益", "年度收益", "风控校验"]:
        assert key in report, f"报告中缺少 {key}"
    data = analyzer.to_dict(risk_limits={"position_limit_pct": 80})
    assert set(data) == {"metrics", "monthly_returns", "yearly_returns", "risk_checks"}
    assert set(data["metrics"]) == {
        "累计收益率", "年化收益率", "年化波动率", "夏普比率", "卡玛比率",
        "最大回撤", "胜率", "盈亏比", "总交易次数",
    }
    json.dumps(data)  # 必须可序列化
    print("  ✓ 报告与导出：文本报告完整、字典可 JSON 序列化")

    print()
    print(report)
    print()
    print("===== 全部测试通过 =====")


if __name__ == "__main__":
    run_test()
