"""单股回测页：设置参数 → 子线程回测 → 净值/回撤曲线 + 绩效指标 + 交易明细"""

import os
import sys

import pandas as pd
import pyqtgraph as pg
from PyQt6.QtCore import QDate, Qt, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDateEdit, QDoubleSpinBox,
                             QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                             QLabel, QLineEdit, QPushButton, QSpinBox, QSplitter,
                             QVBoxLayout, QWidget)

# 保证直接运行本文件时能找到项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from gui.common import (COLOR_HINT, COLOR_ORANGE, MINUTE_HINT, AsyncTask,
                        DataFrameTable, format_metric_value, load_defaults,
                        make_freq_combo, metrics_to_table, plot_drawdown,
                        plot_equity, push_result_task, report_push_result)
from strategies.factory import get_available_strategies

_START_YEARS = 3  # 默认回测区间：近 3 年


def _run_backtest(code, start, end, strategy_key, params, init_cash, commission,
                  slippage, rf, t_plus_1, price_limit, stamp_tax, transfer_fee,
                  freq="daily"):
    """后台任务：下载数据 + 回测 + 绩效分析（在子线程执行，重型库延迟导入）"""
    from core.backtest_engine import BacktestEngine
    from core.risk_analysis import analyze_portfolio, trades_table
    from strategies.factory import create_strategy
    from utils.data_loader import load_market_data

    df = load_market_data(code, start, end, freq=freq)
    strategy = create_strategy(strategy_key, **params)
    entries, exits = strategy.generate_signals(df)
    if entries.sum() == 0:
        raise ValueError("策略没有产生任何买入信号，请换参数或换股票")
    engine = BacktestEngine(
        df, entries, exits, init_cash=init_cash, commission=commission,
        slippage=slippage, rf=rf, code=code, t_plus_1=t_plus_1,
        price_limit=price_limit, stamp_tax=stamp_tax,
        transfer_fee=transfer_fee).run()
    analyzer = analyze_portfolio(engine.portfolio, rf=rf)
    benchmark = df["close"] / df["close"].iloc[0] * init_cash
    return {
        "code": code,
        "equity": engine.portfolio.value(),
        "benchmark": benchmark,
        "drawdown": analyzer.drawdown_series(),
        "metrics": engine.get_metrics(),
        "risk_metrics": analyzer.compute_metrics(),
        "trades": trades_table(engine.portfolio),
        "yearly": analyzer.yearly_returns(),
    }


class BacktestPage(QWidget):
    """单股回测标签页"""

    progress_changed = pyqtSignal(int, str)  # (进度百分比, 描述)，-1 = 不确定

    def __init__(self, win):
        super().__init__()
        self._win = win
        self._task = None
        self._push_task = None
        self._last_result = None
        self._last_info = {}          # 本次回测的基本信息（代码/策略/区间），推送摘要用
        self._strategies = get_available_strategies()
        self._param_widgets = {}

        defaults = load_defaults()
        self._build_ui(defaults)
        self.progress_changed.connect(win.set_progress)

    # ---------- 界面搭建 ----------

    def _build_ui(self, d):
        root = QVBoxLayout(self)

        # 左侧：参数设置
        settings = QGroupBox("回测设置")
        form = QFormLayout(settings)
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("如 600519")
        self.start_edit = QDateEdit(QDate.currentDate().addYears(-_START_YEARS))
        self.end_edit = QDateEdit(QDate.currentDate())
        for e in (self.start_edit, self.end_edit):
            e.setCalendarPopup(True)
            e.setDisplayFormat("yyyy-MM-dd")
        self.strategy_combo = QComboBox()
        for info in self._strategies:
            self.strategy_combo.addItem(info["name"], info["key"])
        self.strategy_combo.currentIndexChanged.connect(self._rebuild_param_form)
        self.init_cash_spin = QDoubleSpinBox()
        self.init_cash_spin.setRange(10_000, 1e12)
        self.init_cash_spin.setDecimals(0)
        self.init_cash_spin.setGroupSeparatorShown(True)
        self.init_cash_spin.setValue(d["init_cash"])
        self.commission_spin = self._make_double_spin(0.0, 0.1, 5, 0.00005, d["commission"])
        self.slippage_spin = self._make_double_spin(0.0, 0.1, 4, 0.0005, d["slippage"])
        self.rf_spin = self._make_double_spin(0.0, 0.2, 3, 0.005, 0.0)

        form.addRow("股票代码", self.code_edit)
        form.addRow("开始日期", self.start_edit)
        form.addRow("结束日期", self.end_edit)
        self.freq_combo = make_freq_combo()
        form.addRow("数据频率", self.freq_combo)
        form.addRow("策略", self.strategy_combo)
        form.addRow("初始资金（元）", self.init_cash_spin)
        form.addRow("佣金费率", self.commission_spin)
        form.addRow("滑点", self.slippage_spin)
        form.addRow("无风险利率", self.rf_spin)

        # 策略参数（随策略切换重建）
        param_box = QGroupBox("策略参数")
        self._param_form = QFormLayout(param_box)

        # A股交易规则
        rules_box = QGroupBox("A股交易规则")
        rules_layout = QVBoxLayout(rules_box)
        self.rule_t1 = QCheckBox("T+1（当日买入次日才能卖出）")
        self.rule_limit = QCheckBox("涨跌停限制（封板信号顺延）")
        self.rule_stamp = QCheckBox("印花税（卖出 0.05%）")
        self.rule_transfer = QCheckBox("过户费（0.001%）")
        for cb in (self.rule_t1, self.rule_limit, self.rule_stamp, self.rule_transfer):
            cb.setChecked(True)
            rules_layout.addWidget(cb)

        hint = QLabel("策略参数根据所选策略自动生成；字符串类参数（如布林带离场方式）使用策略默认值。")
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        hint.setStyleSheet(f"color: {COLOR_HINT};")

        # 分钟线提示：选分钟频率时显示
        self.minute_hint = QLabel(MINUTE_HINT)
        self.minute_hint.setWordWrap(True)
        self.minute_hint.setStyleSheet(f"color: {COLOR_ORANGE};")
        self.minute_hint.setVisible(False)
        self.freq_combo.currentIndexChanged.connect(
            lambda: self.minute_hint.setVisible(
                self.freq_combo.currentData() != "daily"))

        self.run_btn = QPushButton("▶ 开始回测")
        self.run_btn.clicked.connect(self._on_run)
        self.export_btn = QPushButton("导出净值 CSV")
        self.export_btn.setObjectName("secondary")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._export_equity)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.run_btn, 1)
        btn_row.addWidget(self.export_btn)

        # 消息推送：回测完成后自动推送 + 手动推送按钮（配置来自 .env，见设置页）
        self.auto_push_check = QCheckBox("回测完成后自动推送结果")
        self.auto_push_check.setToolTip(
            "勾选后每次回测完成自动把结果摘要推送到已配置的渠道（邮件/企业微信，.env 配置）")
        self.push_btn = QPushButton("📤 推送本次结果")
        self.push_btn.setObjectName("secondary")
        self.push_btn.setEnabled(False)
        self.push_btn.clicked.connect(self._on_push)
        push_row = QHBoxLayout()
        push_row.addWidget(self.auto_push_check)
        push_row.addWidget(self.push_btn)

        left = QVBoxLayout()
        left.addWidget(settings)
        left.addWidget(param_box)
        left.addWidget(rules_box)
        left.addWidget(hint)
        left.addWidget(self.minute_hint)
        left.addLayout(btn_row)
        left.addLayout(push_row)
        left.addStretch(1)
        left_widget = QWidget()
        left_widget.setLayout(left)
        left_widget.setMaximumWidth(420)

        # 右侧：图表 + 表格
        self.equity_plot = pg.PlotWidget()
        self.equity_plot.setMinimumHeight(240)
        self.dd_plot = pg.PlotWidget()
        self.dd_plot.setMinimumHeight(200)
        equity_box = QGroupBox("净值曲线（策略 vs 买入持有）")
        eq_layout = QVBoxLayout(equity_box)
        eq_layout.addWidget(self.equity_plot)
        dd_box = QGroupBox("回撤曲线")
        dd_layout = QVBoxLayout(dd_box)
        dd_layout.addWidget(self.dd_plot)
        charts = QSplitter(Qt.Orientation.Horizontal)
        charts.addWidget(equity_box)
        charts.addWidget(dd_box)
        charts.setSizes([600, 600])

        self.metrics_table = DataFrameTable()
        self.trades_table = DataFrameTable()
        metrics_box = QGroupBox("绩效指标")
        m_layout = QVBoxLayout(metrics_box)
        m_layout.addWidget(self.metrics_table)
        trades_box = QGroupBox("交易明细（右键可导出）")
        t_layout = QVBoxLayout(trades_box)
        t_layout.addWidget(self.trades_table)
        tables = QSplitter(Qt.Orientation.Horizontal)
        tables.addWidget(metrics_box)
        tables.addWidget(trades_box)
        tables.setSizes([380, 820])

        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(charts)
        right.addWidget(tables)
        right.setSizes([430, 330])

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left_widget)
        splitter.addWidget(right)
        splitter.setSizes([390, 990])
        root.addWidget(splitter)

        self._rebuild_param_form()

    def _make_double_spin(self, lo, hi, decimals, step, value):
        w = QDoubleSpinBox()
        w.setRange(lo, hi)
        w.setDecimals(decimals)
        w.setSingleStep(step)
        w.setValue(value)
        return w

    # ---------- 策略参数表单 ----------

    def _rebuild_param_form(self):
        while self._param_form.rowCount():
            self._param_form.removeRow(0)
        self._param_widgets = {}
        info = self._strategies[self.strategy_combo.currentIndex()]
        for key, default in info["default_params"].items():
            help_text = info["params_help"].get(key, key)
            if isinstance(default, bool):
                w = QCheckBox("启用")
                w.setChecked(bool(default))
            elif isinstance(default, int):
                w = QSpinBox()
                w.setRange(1, 1000)
                w.setValue(int(default))
            elif isinstance(default, str):
                w = QComboBox()
                w.addItem("跌破中轨卖出（mid）", "mid")
                w.addItem("跌破下轨卖出（lower）", "lower")
                w.setCurrentIndex(max(w.findData(default), 0))
            else:
                w = QDoubleSpinBox()
                w.setRange(0.01, 1000.0)
                w.setDecimals(2)
                w.setSingleStep(0.5 if float(default) < 10 else 1.0)
                w.setValue(float(default))
            self._param_widgets[key] = w
            self._param_form.addRow(f"{help_text}（{key}）", w)

    def _collect_params(self) -> dict:
        params = {}
        for key, w in self._param_widgets.items():
            if isinstance(w, QCheckBox):
                params[key] = w.isChecked()
            elif isinstance(w, QComboBox):
                params[key] = w.currentData()
            else:
                params[key] = w.value()
        return params

    # ---------- 任务控制 ----------

    def set_running(self, running: bool):
        self.run_btn.setEnabled(not running)
        self.run_btn.setText("⏳ 回测运行中…" if running else "▶ 开始回测")
        if running:
            self.progress_changed.emit(-1, "回测任务进行中…")

    def _on_run(self):
        if self._task is not None and self._task.thread.isRunning():
            return
        code = self.code_edit.text().strip()
        if not code:
            self._win.log("[提示] 请先填写股票代码")
            return
        if self.start_edit.date() > self.end_edit.date():
            self._win.log("[提示] 开始日期不能晚于结束日期")
            return
        strategy_key = self.strategy_combo.currentData()
        params = self._collect_params()
        freq = self.freq_combo.currentData()
        self._last_info = {
            "code": code,
            "策略": self.strategy_combo.currentText(),
            "频率": self.freq_combo.currentText(),
            "start": self.start_edit.date().toString("yyyy-MM-dd"),
            "end": self.end_edit.date().toString("yyyy-MM-dd"),
            "init_cash": self.init_cash_spin.value(),
        }
        self.push_btn.setEnabled(False)  # 结果已过期，等新结果出来再允许推送
        self._win.log(f"开始回测 {code}（策略：{self.strategy_combo.currentText()}，"
                      f"频率 {self._last_info['频率']}，"
                      f"区间 {self._last_info['start']} ~ {self._last_info['end']}）")
        if freq != "daily":
            self._win.log(f"[提示] {MINUTE_HINT}")
        self._task = AsyncTask(
            self, _run_backtest,
            code, self.start_edit.date().toPyDate(), self.end_edit.date().toPyDate(),
            strategy_key, params, self.init_cash_spin.value(),
            self.commission_spin.value(), self.slippage_spin.value(),
            self.rf_spin.value(), self.rule_t1.isChecked(),
            self.rule_limit.isChecked(), self.rule_stamp.isChecked(),
            self.rule_transfer.isChecked(), freq,
            on_finished=self._show_result)
        self._task.start()

    # ---------- 结果展示 ----------

    def _show_result(self, result):
        self._last_result = result
        plot_equity(self.equity_plot, result["equity"], result["benchmark"])
        plot_drawdown(self.dd_plot, result["drawdown"])
        self.metrics_table.set_export_name(f"{result['code']}_绩效指标")
        self.metrics_table.set_dataframe(
            metrics_to_table(result["metrics"], result["risk_metrics"]))
        self.trades_table.set_export_name(f"{result['code']}_交易明细")
        self.trades_table.set_dataframe(result["trades"])
        self.export_btn.setEnabled(True)
        self.push_btn.setEnabled(True)
        m = result["metrics"]
        self._win.log(f"{result['code']} 回测完成：总交易 {m['总交易次数']} 次，"
                      f"累计收益 {m['累计收益率'] * 100:.2f}%，"
                      f"最大回撤 {m['最大回撤'] * 100:.2f}%")
        self._win.finish_progress("回测完成")
        if self.auto_push_check.isChecked():
            self._on_push()  # 勾选了「完成后自动推送」就自动发一次

    def _export_equity(self):
        r = self._last_result
        if r is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出净值", f"{r['code']}_净值.csv", "CSV 文件 (*.csv)")
        if not path:
            return
        out = pd.DataFrame({
            "日期": r["equity"].index.strftime("%Y-%m-%d"),
            "策略净值": r["equity"].values,
            "买入持有": r["benchmark"].values,
        })
        out.to_csv(path, index=False, encoding="utf-8-sig")
        self._win.log(f"已导出净值：{path}")

    # ---------- 消息推送 ----------

    def _build_summary(self) -> dict:
        """把最近一次回测的关键结果整理成推送摘要（值都是字符串，可直接发消息）"""
        r = self._last_result
        info = self._last_info or {}
        merged = {**r["risk_metrics"], **r["metrics"]}
        return {
            "任务类型": "单股回测",
            "股票代码": str(info.get("code", "")),
            "策略名称": str(info.get("策略", "")),
            "数据频率": str(info.get("频率", "日线")),
            "回测区间": f"{info.get('start', '')} ~ {info.get('end', '')}",
            "初始资金": f"{info.get('init_cash', 0):,.0f} 元",
            "累计收益率": format_metric_value("累计收益率", merged.get("累计收益率")),
            "年化收益率": format_metric_value("年化收益率", merged.get("年化收益率")),
            "最大回撤": format_metric_value("最大回撤", merged.get("最大回撤")),
            "夏普比率": format_metric_value("夏普比率", merged.get("夏普比率")),
            "胜率": format_metric_value("胜率", merged.get("胜率")),
            "总交易次数": format_metric_value("总交易次数", merged.get("总交易次数")),
            "期末总资产": format_metric_value("期末总资产", merged.get("期末总资产")),
            "基准收益率": format_metric_value("基准收益率", merged.get("基准收益率")),
        }

    def _on_push(self):
        if self._push_task is not None and self._push_task.thread.isRunning():
            return
        if self._last_result is None:
            return
        self.push_btn.setEnabled(False)
        self.push_btn.setText("⏳ 推送中…")
        self._win.log("[推送] 正在推送本次回测结果…")
        self._push_task = AsyncTask(self, push_result_task, self._build_summary(),
                                    on_finished=self._push_done, manage_running=False)
        self._push_task.start()

    def _push_done(self, result):
        self.push_btn.setEnabled(True)
        self.push_btn.setText("📤 推送本次结果")
        self._win.finish_progress("推送完成")
        report_push_result(self, result, self._win.log)

    # ---------- 供参数寻优页一键应用 ----------

    def apply_settings(self, code, start, end, strategy_key, params,
                       freq="daily", init_cash=None, commission=None,
                       slippage=None, rules=None):
        """把一组设置填到本页（参数寻优页「用最优参数回测」调用）

        参数:
            code/start/end:  股票代码、起止日期（datetime.date）
            strategy_key:    策略 key（类名，如 "DoubleMAStrategy"）
            params:          策略参数字典（最优参数）
            freq:            数据频率（"daily" / "1min" / ...）
            init_cash/commission/slippage: 可选，全局回测参数
            rules:           可选，(T+1, 涨跌停, 印花税, 过户费) 四个布尔开关
        """
        from PyQt6.QtCore import QDate
        self.code_edit.setText(code)
        self.start_edit.setDate(QDate(start.year, start.month, start.day))
        self.end_edit.setDate(QDate(end.year, end.month, end.day))
        fi = self.freq_combo.findData(freq)
        if fi >= 0:
            self.freq_combo.setCurrentIndex(fi)
        si = self.strategy_combo.findData(strategy_key)
        if si < 0:
            raise ValueError(f"回测页没有该策略：{strategy_key}")
        self.strategy_combo.setCurrentIndex(si)  # 触发策略参数表单重建
        for key, val in (params or {}).items():
            w = self._param_widgets.get(key)
            if w is None:
                continue
            if isinstance(w, QCheckBox):
                w.setChecked(bool(val))
            elif isinstance(w, QComboBox):
                i = w.findData(val)
                if i >= 0:
                    w.setCurrentIndex(i)
            else:
                w.setValue(val)  # QSpinBox / QDoubleSpinBox 通用
        if init_cash is not None:
            self.init_cash_spin.setValue(init_cash)
        if commission is not None:
            self.commission_spin.setValue(commission)
        if slippage is not None:
            self.slippage_spin.setValue(slippage)
        if rules:
            for cb, v in zip((self.rule_t1, self.rule_limit,
                              self.rule_stamp, self.rule_transfer), rules):
                cb.setChecked(bool(v))
