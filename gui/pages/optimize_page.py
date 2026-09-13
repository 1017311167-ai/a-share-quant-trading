"""参数寻优页：网格搜索 / 遗传算法，在子线程运行，实时进度条"""

import os
import sys

from PyQt6.QtCore import QDate, Qt, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDateEdit, QDoubleSpinBox,
                             QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                             QLabel, QLineEdit, QMessageBox, QPushButton,
                             QSpinBox, QSplitter, QVBoxLayout, QWidget)

# 保证直接运行本文件时能找到项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from gui.common import (COLOR_ORANGE, MINUTE_HINT, OPTIMIZE_METRICS, AsyncTask,
                        DataFrameTable, load_defaults, make_freq_combo,
                        metrics_to_table)
from strategies.factory import get_available_strategies


def _default_range(default):
    """按参数默认值推导一个合理的寻优范围（与网页版一致）"""
    if isinstance(default, int):
        return (max(1, default - 5), default + 10, 5)
    d = float(default)
    if d < 10:
        return (max(0.1, d - 1.0), d + 1.0, 0.5)
    return (max(1.0, d - 10.0), d + 10.0, 5.0)


def _run_optimize(code, start, end, strategy_name, metric, method, ranges,
                  init_cash, commission, slippage, rf, t_plus_1, price_limit,
                  stamp_tax, transfer_fee, max_workers, max_combos, pop_size,
                  n_generations, cxpb, mutpb, seed, freq="daily",
                  progress_cb=None):
    """后台任务：下载数据 + 参数寻优（重型库延迟导入）"""
    from optimization import optimize_parameters
    from utils.data_loader import load_market_data

    df = load_market_data(code, start, end, freq=freq)
    kwargs = dict(init_cash=init_cash, commission=commission, slippage=slippage,
                  rf=rf, t_plus_1=t_plus_1, price_limit=price_limit,
                  stamp_tax=stamp_tax, transfer_fee=transfer_fee,
                  code=code, max_workers=max_workers, progress_cb=progress_cb)
    if method == "grid":
        kwargs["max_combos"] = max_combos
    else:
        kwargs.update(pop_size=pop_size, n_generations=n_generations,
                      cxpb=cxpb, mutpb=mutpb, seed=seed)
    return optimize_parameters(df, strategy_name, ranges, metric=metric,
                               method=method, **kwargs)


class OptimizePage(QWidget):
    """参数寻优标签页"""

    progress_changed = pyqtSignal(int, str)

    def __init__(self, win):
        super().__init__()
        self._win = win
        self._task = None
        self._strategies = get_available_strategies()
        self._range_widgets = {}   # 参数名 -> {"min": spin, "max": spin, "step": spin}

        d = load_defaults()
        self._build_ui(d)
        self.progress_changed.connect(win.set_progress)

    # ---------- 界面搭建 ----------

    def _build_ui(self, d):
        root = QVBoxLayout(self)

        settings = QGroupBox("寻优设置")
        form = QFormLayout(settings)
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("如 600519")
        self.start_edit = QDateEdit(QDate.currentDate().addYears(-3))
        self.end_edit = QDateEdit(QDate.currentDate())
        for e in (self.start_edit, self.end_edit):
            e.setCalendarPopup(True)
            e.setDisplayFormat("yyyy-MM-dd")
        self.strategy_combo = QComboBox()
        for info in self._strategies:
            self.strategy_combo.addItem(info["name"], info["key"])
        self.strategy_combo.currentIndexChanged.connect(self._rebuild_range_form)
        self.metric_combo = QComboBox()
        self.metric_combo.addItems(OPTIMIZE_METRICS)
        self.metric_combo.setCurrentText("夏普比率")
        self.method_combo = QComboBox()
        self.method_combo.addItem("网格搜索（穷举所有组合）", "grid")
        self.method_combo.addItem("遗传算法（进化寻优）", "genetic")
        self.method_combo.currentIndexChanged.connect(self._toggle_method)
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 32)
        self.workers_spin.setValue(d["max_workers"])

        form.addRow("股票代码", self.code_edit)
        form.addRow("开始日期", self.start_edit)
        form.addRow("结束日期", self.end_edit)
        self.freq_combo = make_freq_combo()
        form.addRow("数据频率", self.freq_combo)
        form.addRow("策略", self.strategy_combo)
        form.addRow("目标指标", self.metric_combo)
        form.addRow("寻优方法", self.method_combo)
        form.addRow("并发进程数", self.workers_spin)

        # 参数范围（随策略切换重建）
        range_box = QGroupBox("参数范围")
        self._range_form = QFormLayout(range_box)

        # 回测全局参数（与单股回测页一致）
        global_box = QGroupBox("回测全局参数")
        gform = QFormLayout(global_box)
        self.init_cash_spin = QDoubleSpinBox()
        self.init_cash_spin.setRange(10_000, 1e12)
        self.init_cash_spin.setDecimals(0)
        self.init_cash_spin.setGroupSeparatorShown(True)
        self.init_cash_spin.setValue(d["init_cash"])
        self.commission_spin = QDoubleSpinBox()
        self.commission_spin.setRange(0.0, 0.1)
        self.commission_spin.setDecimals(5)
        self.commission_spin.setSingleStep(0.00005)
        self.commission_spin.setValue(d["commission"])
        self.slippage_spin = QDoubleSpinBox()
        self.slippage_spin.setRange(0.0, 0.1)
        self.slippage_spin.setDecimals(4)
        self.slippage_spin.setSingleStep(0.0005)
        self.slippage_spin.setValue(d["slippage"])
        gform.addRow("初始资金（元）", self.init_cash_spin)
        gform.addRow("佣金费率", self.commission_spin)
        gform.addRow("滑点", self.slippage_spin)
        rules_row = QVBoxLayout()
        self.rule_t1 = QCheckBox("T+1 交易规则")
        self.rule_limit = QCheckBox("涨跌停限制")
        self.rule_stamp = QCheckBox("印花税")
        self.rule_transfer = QCheckBox("过户费")
        for cb in (self.rule_t1, self.rule_limit, self.rule_stamp, self.rule_transfer):
            cb.setChecked(True)
            rules_row.addWidget(cb)
        rules_widget = QWidget()
        rules_widget.setLayout(rules_row)
        gform.addRow("A股规则", rules_widget)

        # 网格搜索选项
        self.grid_box = QGroupBox("网格搜索选项")
        gform = QFormLayout(self.grid_box)
        self.max_combos_spin = QSpinBox()
        self.max_combos_spin.setRange(10, 2000)
        self.max_combos_spin.setValue(200)
        gform.addRow("最大组合数上限", self.max_combos_spin)

        # 遗传算法选项
        self.ga_box = QGroupBox("遗传算法选项")
        gaform = QFormLayout(self.ga_box)
        gaform.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.pop_spin = QSpinBox()
        self.pop_spin.setRange(5, 200)
        self.pop_spin.setValue(20)
        self.gens_spin = QSpinBox()
        self.gens_spin.setRange(1, 100)
        self.gens_spin.setValue(10)
        self.cxpb_spin = QDoubleSpinBox()
        self.cxpb_spin.setRange(0.0, 1.0)
        self.cxpb_spin.setDecimals(2)
        self.cxpb_spin.setSingleStep(0.05)
        self.cxpb_spin.setValue(0.5)
        self.mutpb_spin = QDoubleSpinBox()
        self.mutpb_spin.setRange(0.0, 1.0)
        self.mutpb_spin.setDecimals(2)
        self.mutpb_spin.setSingleStep(0.05)
        self.mutpb_spin.setValue(0.2)
        self.seed_check = QCheckBox("固定随机种子（结果可复现）")
        self.seed_spin = QSpinBox()
        self.seed_spin.setRange(0, 99999)
        self.seed_spin.setValue(42)
        self.seed_spin.setEnabled(False)
        self.seed_check.toggled.connect(self.seed_spin.setEnabled)
        seed_row = QHBoxLayout()
        seed_row.addWidget(self.seed_check)
        seed_row.addWidget(self.seed_spin, 1)
        gaform.addRow("种群大小", self.pop_spin)
        gaform.addRow("迭代次数（代数）", self.gens_spin)
        gaform.addRow("交叉概率", self.cxpb_spin)
        gaform.addRow("变异概率", self.mutpb_spin)
        gaform.addRow("随机种子", self._seed_row_widget(seed_row))

        self.run_btn = QPushButton("▶ 开始寻优")
        self.run_btn.clicked.connect(self._on_run)
        self.export_btn = QPushButton("导出结果 CSV")
        self.export_btn.setObjectName("secondary")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._export_results)
        self.apply_btn = QPushButton("▶ 用最优参数回测")
        self.apply_btn.setEnabled(False)
        self.apply_btn.setToolTip(
            "把本次寻优出的最优参数一键填到「单股回测」页并自动运行")
        self.apply_btn.clicked.connect(self._apply_best)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.run_btn, 1)
        btn_row.addWidget(self.export_btn)
        btn_row.addWidget(self.apply_btn)

        # 分钟线提示（仅选择分钟频率时显示）
        self.minute_hint = QLabel(MINUTE_HINT)
        self.minute_hint.setWordWrap(True)
        self.minute_hint.setStyleSheet(f"color: {COLOR_ORANGE};")
        self.minute_hint.setVisible(False)
        self.freq_combo.currentIndexChanged.connect(
            lambda: self.minute_hint.setVisible(
                self.freq_combo.currentData() != "daily"))

        left = QVBoxLayout()
        left.addWidget(settings)
        left.addWidget(self.minute_hint)
        left.addWidget(range_box)
        left.addWidget(global_box)
        left.addWidget(self.grid_box)
        left.addWidget(self.ga_box)
        left.addLayout(btn_row)
        left.addStretch(1)
        left_widget = QWidget()
        left_widget.setLayout(left)
        left_widget.setMaximumWidth(430)

        # 右侧：最优结果 + 全部组合表
        self.best_label = QLabel("最优参数：—")
        self.best_label.setObjectName("big")
        self.best_value_label = QLabel("")
        self.failed_label = QLabel("")
        self.failed_label.setObjectName("hint")
        head = QVBoxLayout()
        head.addWidget(self.best_label)
        head.addWidget(self.best_value_label)
        head.addWidget(self.failed_label)

        self.best_table = DataFrameTable()
        best_box = QGroupBox("最优组合绩效")
        b_layout = QVBoxLayout(best_box)
        b_layout.addLayout(head)
        b_layout.addWidget(self.best_table)

        self.results_table = DataFrameTable()
        results_box = QGroupBox("全部组合结果（点击表头排序，右键导出）")
        r_layout = QVBoxLayout(results_box)
        r_layout.addWidget(self.results_table)

        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(best_box)
        right.addWidget(results_box)
        right.setSizes([300, 460])

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left_widget)
        splitter.addWidget(right)
        splitter.setSizes([400, 980])
        root.addWidget(splitter)

        self._rebuild_range_form()
        self._toggle_method()

    def _seed_row_widget(self, row_layout) -> QWidget:
        w = QWidget()
        w.setLayout(row_layout)
        return w

    def _toggle_method(self):
        is_grid = self.method_combo.currentData() == "grid"
        self.grid_box.setEnabled(is_grid)
        self.ga_box.setEnabled(not is_grid)

    # ---------- 参数范围表单 ----------

    def _rebuild_range_form(self):
        while self._range_form.rowCount():
            self._range_form.removeRow(0)
        self._range_widgets = {}
        info = self._strategies[self.strategy_combo.currentIndex()]
        for key, default in info["default_params"].items():
            if isinstance(default, bool):
                continue  # 开关类参数用策略默认值
            if isinstance(default, str):
                continue  # 字符串参数（如离场方式）用策略默认值
            lo, hi, step = _default_range(default)
            if isinstance(default, int):
                w_min, w_max = QSpinBox(), QSpinBox()
                for w, v in ((w_min, lo), (w_max, hi)):
                    w.setRange(1, 5000)
                    w.setValue(int(v))
                w_step = QSpinBox()
                w_step.setRange(1, 200)
                w_step.setValue(int(step))
            else:
                w_min, w_max = QDoubleSpinBox(), QDoubleSpinBox()
                for w, v in ((w_min, lo), (w_max, hi)):
                    w.setRange(0.01, 10000.0)
                    w.setDecimals(2)
                    w.setValue(float(v))
                w_step = QDoubleSpinBox()
                w_step.setRange(0.01, 100.0)
                w_step.setDecimals(2)
                w_step.setValue(float(step))
            self._range_widgets[key] = {"min": w_min, "max": w_max, "step": w_step}
            row = QHBoxLayout()
            row.addWidget(QLabel("最小"))
            row.addWidget(w_min, 1)
            row.addWidget(QLabel("最大"))
            row.addWidget(w_max, 1)
            row.addWidget(QLabel("步长"))
            row.addWidget(w_step, 1)
            row_widget = QWidget()
            row_widget.setLayout(row)
            help_text = info["params_help"].get(key, key)
            self._range_form.addRow(f"{help_text}（{key}）", row_widget)

    def _collect_ranges(self) -> dict:
        """收集参数范围；不合法返回 (None, 错误信息)"""
        ranges = {}
        for key, ws in self._range_widgets.items():
            lo = ws["min"].value()
            hi = ws["max"].value()
            step = ws["step"].value()
            if lo > hi:
                return None, f"参数 {key} 的最小值不能大于最大值"
            if step <= 0:
                return None, f"参数 {key} 的步长必须大于 0"
            ranges[key] = {"min": lo, "max": hi, "step": step}
        if not ranges:
            return None, "当前策略没有可寻优的数值参数"
        return ranges, None

    # ---------- 任务控制 ----------

    def set_running(self, running: bool):
        self.run_btn.setEnabled(not running)
        self.run_btn.setText("⏳ 寻优运行中…" if running else "▶ 开始寻优")
        if running:
            self.progress_changed.emit(-1, "准备数据…")

    def _progress_cb(self, done, total):
        pct = int(done / total * 100) if total else -1
        self.progress_changed.emit(pct, f"已回测 {done}/{total} 组")

    def _on_run(self):
        if self._task is not None and self._task.thread.isRunning():
            return
        code = self.code_edit.text().strip()
        if not code:
            self._win.log("[提示] 请先填写股票代码")
            return
        ranges, err = self._collect_ranges()
        if err:
            self._win.log(f"[提示] {err}")
            return
        method = self.method_combo.currentData()
        seed = self.seed_spin.value() if self.seed_check.isChecked() else None
        self._win.log(f"开始参数寻优：{code} × {self.strategy_combo.currentText()}，"
                      f"方法 {self.method_combo.currentText()}，"
                      f"目标 {self.metric_combo.currentText()}，"
                      f"范围 {ranges}")
        self._task = AsyncTask(
            self, _run_optimize,
            code, self.start_edit.date().toPyDate(), self.end_edit.date().toPyDate(),
            self.strategy_combo.currentData(), self.metric_combo.currentText(),
            method, ranges, self.init_cash_spin.value(),
            self.commission_spin.value(), self.slippage_spin.value(), 0.0,
            self.rule_t1.isChecked(), self.rule_limit.isChecked(),
            self.rule_stamp.isChecked(), self.rule_transfer.isChecked(),
            self.workers_spin.value(),
            self.max_combos_spin.value(), self.pop_spin.value(),
            self.gens_spin.value(), self.cxpb_spin.value(),
            self.mutpb_spin.value(), seed, self.freq_combo.currentData(),
            progress_cb=self._progress_cb,
            on_finished=self._show_result)
        self._task.start()

    # ---------- 结果展示 ----------

    def _show_result(self, out):
        self._out = out
        self.apply_btn.setEnabled(False)  # 新结果出来前禁止一键应用
        bp = out.get("best_params") or {}
        if not bp:
            # 全部参数组合都无效（遗传算法找不到可行解时 best_params 为空）
            self.best_label.setText("最优参数：寻优失败")
            self.best_value_label.setText("没有找到有效参数组合")
            self.best_table.set_dataframe(None)
            self.results_table.set_export_name("参数寻优结果")
            self.results_table.set_dataframe(out["results"])
            self.failed_label.setText(
                f"全部 {len(out['failed'])} 组组合都无效，"
                "请放宽参数范围、延长回测区间或换策略")
            self.export_btn.setEnabled(not out["results"].empty)
            self._win.log("[提示] 参数寻优失败：没有找到有效参数组合")
            self._win.finish_progress("参数寻优失败")
            QMessageBox.warning(
                self, "参数寻优失败",
                "所有参数组合的回测都失败了，没有找到最优参数。\n\n"
                "建议：放宽参数范围、延长回测区间，或换一个策略再试。")
            return
        params_text = "、".join(f"{k}={v}" for k, v in bp.items())
        self.best_label.setText(f"最优参数：{params_text}")
        self.best_value_label.setText(
            f"最优{out['metric']}：{out['best_value']:.4f}"
            f"（共评估 {len(out['results'])} 组有效组合）")
        self.best_table.set_export_name("最优组合绩效")
        self.best_table.set_dataframe(metrics_to_table(out["best_metrics"]))
        self.results_table.set_export_name("参数寻优结果")
        self.results_table.set_dataframe(out["results"])
        if out["failed"]:
            self.failed_label.setText(f"无效组合 {len(out['failed'])} 组（已自动跳过）")
        else:
            self.failed_label.setText("全部组合回测成功")
        self.export_btn.setEnabled(True)
        self.apply_btn.setEnabled(True)
        self._win.log(f"参数寻优完成：最优 {params_text}，"
                      f"{out['metric']} = {out['best_value']:.4f}")
        self._win.finish_progress("参数寻优完成")

    def _apply_best(self):
        """把最优参数一键填到「单股回测」页并自动运行"""
        out = self._out
        if not out or not out.get("best_params"):
            return
        try:
            self._win.backtest_page.apply_settings(
                self.code_edit.text().strip(),
                self.start_edit.date().toPyDate(),
                self.end_edit.date().toPyDate(),
                self.strategy_combo.currentData(),
                out["best_params"],
                freq=self.freq_combo.currentData(),
                init_cash=self.init_cash_spin.value(),
                commission=self.commission_spin.value(),
                slippage=self.slippage_spin.value(),
                rules=(self.rule_t1.isChecked(), self.rule_limit.isChecked(),
                       self.rule_stamp.isChecked(), self.rule_transfer.isChecked()))
        except Exception as e:
            QMessageBox.warning(self, "无法应用最优参数", f"应用失败：{e}")
            return
        # 切到单股回测页并自动开始回测
        tabs = self._win.tabs
        for i in range(tabs.count()):
            if tabs.widget(i) is self._win.backtest_page:
                tabs.setCurrentIndex(i)
                break
        self._win.backtest_page._on_run()

    def _export_results(self):
        self.results_table.export_csv_dialog()
