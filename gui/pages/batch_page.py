"""批量回测页：多只股票 × 多个策略并发回测，汇总表 + Excel 报告导出"""

import os
import sys

from PyQt6.QtCore import QDate, Qt, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QDateEdit, QDoubleSpinBox, QFileDialog,
                             QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                             QListWidget, QListWidgetItem, QPlainTextEdit,
                             QPushButton, QSpinBox, QSplitter, QVBoxLayout,
                             QWidget)

# 保证直接运行本文件时能找到项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from gui.common import (COLOR_ORANGE, MINUTE_HINT, AsyncTask, DataFrameTable,
                        load_defaults, make_freq_combo, push_result_task,
                        report_push_result)
from strategies.factory import get_available_strategies


def _run_batch(codes, strategy_configs, start, end, init_cash, commission,
               slippage, rf, t_plus_1, price_limit, stamp_tax, transfer_fee,
               max_workers, freq="daily", progress_cb=None):
    """后台任务：批量回测（重型库延迟导入）"""
    from core.batch_backtest import run_batch
    return run_batch(
        codes, strategy_configs, init_cash=init_cash, commission=commission,
        slippage=slippage, start=start, end=end, rf=rf,
        t_plus_1=t_plus_1, price_limit=price_limit, stamp_tax=stamp_tax,
        transfer_fee=transfer_fee, max_workers=max_workers, freq=freq,
        progress_cb=progress_cb)


class BatchPage(QWidget):
    """批量回测标签页"""

    progress_changed = pyqtSignal(int, str)

    def __init__(self, win):
        super().__init__()
        self._win = win
        self._task = None
        self._push_task = None
        self._out = None
        self._strategies = get_available_strategies()

        d = load_defaults()
        self._build_ui(d)
        self.progress_changed.connect(win.set_progress)

    # ---------- 界面搭建 ----------

    def _build_ui(self, d):
        root = QVBoxLayout(self)

        settings = QGroupBox("批量回测设置")
        form = QFormLayout(settings)
        self.codes_edit = QPlainTextEdit()
        self.codes_edit.setPlaceholderText("每行一个股票代码，如：\n600519\n000001\n300750")
        self.codes_edit.setMaximumHeight(110)
        self.start_edit = QDateEdit(QDate.currentDate().addYears(-3))
        self.end_edit = QDateEdit(QDate.currentDate())
        for e in (self.start_edit, self.end_edit):
            e.setCalendarPopup(True)
            e.setDisplayFormat("yyyy-MM-dd")
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
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 32)
        self.workers_spin.setValue(d["max_workers"])

        form.addRow("股票代码", self.codes_edit)
        form.addRow("开始日期", self.start_edit)
        form.addRow("结束日期", self.end_edit)
        self.freq_combo = make_freq_combo()
        form.addRow("数据频率", self.freq_combo)
        form.addRow("初始资金（元）", self.init_cash_spin)
        form.addRow("佣金费率", self.commission_spin)
        form.addRow("滑点", self.slippage_spin)
        form.addRow("并发进程数", self.workers_spin)

        # 策略多选
        strat_box = QGroupBox("参与回测的策略（可多选，用默认参数）")
        s_layout = QVBoxLayout(strat_box)
        self.strategy_list = QListWidget()
        for info in self._strategies:
            item = QListWidgetItem(info["name"])
            item.setData(Qt.ItemDataRole.UserRole, info["key"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.strategy_list.addItem(item)
        s_layout.addWidget(self.strategy_list)

        rules_box = QGroupBox("A股交易规则")
        rules_layout = QVBoxLayout(rules_box)
        self.rule_t1 = QCheckBox("T+1 交易规则")
        self.rule_limit = QCheckBox("涨跌停限制")
        self.rule_stamp = QCheckBox("印花税")
        self.rule_transfer = QCheckBox("过户费")
        for cb in (self.rule_t1, self.rule_limit, self.rule_stamp, self.rule_transfer):
            cb.setChecked(True)
            rules_layout.addWidget(cb)

        self.run_btn = QPushButton("▶ 开始批量回测")
        self.run_btn.clicked.connect(self._on_run)
        self.export_btn = QPushButton("导出 Excel 报告")
        self.export_btn.setObjectName("secondary")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._export_excel)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.run_btn, 1)
        btn_row.addWidget(self.export_btn)

        # 消息推送：批量回测完成后自动推送汇总 + 手动推送按钮
        self.auto_push_check = QCheckBox("批量回测完成后自动推送汇总")
        self.auto_push_check.setToolTip(
            "勾选后批量回测完成自动把汇总结果推送到已配置的渠道（邮件/企业微信，.env 配置）")
        self.push_btn = QPushButton("📤 推送汇总结果")
        self.push_btn.setObjectName("secondary")
        self.push_btn.setEnabled(False)
        self.push_btn.clicked.connect(self._on_push)
        push_row = QHBoxLayout()
        push_row.addWidget(self.auto_push_check)
        push_row.addWidget(self.push_btn)

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
        left.addWidget(strat_box)
        left.addWidget(rules_box)
        left.addLayout(btn_row)
        left.addLayout(push_row)
        left.addStretch(1)
        left_widget = QWidget()
        left_widget.setLayout(left)
        left_widget.setMaximumWidth(430)

        # 右侧：汇总表 + 失败信息
        self.results_table = DataFrameTable()
        results_box = QGroupBox("回测结果汇总（点击表头排序，右键导出）")
        r_layout = QVBoxLayout(results_box)
        r_layout.addWidget(self.results_table)

        self.status_label = QLabel("")
        self.failed_edit = QPlainTextEdit()
        self.failed_edit.setReadOnly(True)
        self.failed_edit.setPlaceholderText("失败的组合会显示在这里（个别股票无数据时自动跳过）")
        failed_box = QGroupBox("失败组合")
        f_layout = QVBoxLayout(failed_box)
        f_layout.addWidget(self.failed_edit)

        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(results_box)
        right.addWidget(failed_box)
        right.setSizes([480, 200])

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left_widget)
        splitter.addWidget(right)
        splitter.setSizes([400, 980])
        root.addWidget(splitter)
        root.addWidget(self.status_label)

    # ---------- 任务控制 ----------

    def set_running(self, running: bool):
        self.run_btn.setEnabled(not running)
        self.run_btn.setText("⏳ 批量回测运行中…" if running else "▶ 开始批量回测")
        if running:
            self.progress_changed.emit(-1, "加载行情数据…")

    def _progress_cb(self, done, total):
        pct = int(done / total * 100) if total else -1
        self.progress_changed.emit(pct, f"已回测 {done}/{total} 个组合")

    def _on_run(self):
        if self._task is not None and self._task.thread.isRunning():
            return
        codes = []
        for line in self.codes_edit.toPlainText().splitlines():
            c = line.strip()
            if c and c not in codes:
                codes.append(c)
        if not codes:
            self._win.log("[提示] 请先填写股票代码（每行一个）")
            return
        configs = []
        for i in range(self.strategy_list.count()):
            item = self.strategy_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                configs.append({"name": item.data(Qt.ItemDataRole.UserRole)})
        if not configs:
            self._win.log("[提示] 请至少勾选一个策略")
            return
        self.push_btn.setEnabled(False)  # 结果已过期，等新结果出来再允许推送
        freq = self.freq_combo.currentData()
        self._last_freq = self.freq_combo.currentText()
        if freq != "daily":
            self._win.log(f"[提示] {MINUTE_HINT}")
        self._win.log(f"开始批量回测：{len(codes)} 只股票 × {len(configs)} 个策略 "
                      f"= {len(codes) * len(configs)} 个组合（{self._last_freq}）")
        self._task = AsyncTask(
            self, _run_batch,
            codes, configs, self.start_edit.date().toPyDate(),
            self.end_edit.date().toPyDate(), self.init_cash_spin.value(),
            self.commission_spin.value(), self.slippage_spin.value(), 0.0,
            self.rule_t1.isChecked(), self.rule_limit.isChecked(),
            self.rule_stamp.isChecked(), self.rule_transfer.isChecked(),
            self.workers_spin.value(), freq,
            progress_cb=self._progress_cb,
            on_finished=self._show_result)
        self._task.start()

    # ---------- 结果展示 ----------

    def _show_result(self, out):
        self._out = out
        self.results_table.set_export_name("批量回测结果")
        self.results_table.set_dataframe(out["results"])
        if out["failed"]:
            self.failed_edit.setPlainText("\n".join(out["failed"]))
        else:
            self.failed_edit.setPlainText("全部组合回测成功")
        self.status_label.setText(
            f"共 {len(out['results'])} 个组合完成，用时 {out['elapsed']:.1f} 秒；"
            f"表格可按任意列排序，右键可导出 CSV")
        self.export_btn.setEnabled(not out["results"].empty)
        self.push_btn.setEnabled(not out["results"].empty)
        self._win.log(f"批量回测完成：{len(out['results'])} 个组合成功，"
                      f"{len(out['failed'])} 个失败，用时 {out['elapsed']:.1f} 秒")
        self._win.finish_progress("批量回测完成")
        if self.auto_push_check.isChecked() and not out["results"].empty:
            self._on_push()  # 勾选了「完成后自动推送」就自动发一次

    def _export_excel(self):
        if self._out is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 Excel 报告", "批量回测报告.xlsx", "Excel 文件 (*.xlsx)")
        if not path:
            return
        from core.batch_backtest import export_excel
        export_excel(self._out, path)
        self._win.log(f"Excel 报告已导出：{path}")

    # ---------- 消息推送 ----------

    def _build_summary(self) -> dict:
        """把最近一次批量回测整理成推送摘要（值都是字符串，可直接发消息）"""
        out = self._out
        results = out["results"]
        if results.empty:
            best = "无"
        else:
            best_row = results.sort_values("夏普比率", ascending=False).iloc[0]
            best = (f"{best_row['股票代码']} + {best_row['策略名称']}："
                    f"夏普 {best_row['夏普比率']:.2f}，"
                    f"累计收益 {best_row['累计收益率'] * 100:.2f}%")
        return {
            "任务类型": "批量回测",
            "数据频率": str(getattr(self, "_last_freq", "日线")),
            "组合总数": f"{len(results) + len(out['failed'])} 个",
            "成功组合数": f"{len(results)} 个",
            "失败组合数": f"{len(out['failed'])} 个",
            "最优组合（按夏普比率）": best,
            "总用时": f"{out['elapsed']:.1f} 秒",
        }

    def _on_push(self):
        if self._push_task is not None and self._push_task.thread.isRunning():
            return
        if self._out is None or self._out["results"].empty:
            return
        self.push_btn.setEnabled(False)
        self.push_btn.setText("⏳ 推送中…")
        self._win.log("[推送] 正在推送批量回测汇总…")
        self._push_task = AsyncTask(self, push_result_task, self._build_summary(),
                                    on_finished=self._push_done, manage_running=False)
        self._push_task.start()

    def _push_done(self, result):
        self.push_btn.setEnabled(True)
        self.push_btn.setText("📤 推送汇总结果")
        self._win.finish_progress("推送完成")
        report_push_result(self, result, self._win.log)
