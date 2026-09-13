"""主窗口：QTabWidget 分 5 个标签页 + 底部日志框 + 状态栏进度条"""

import logging
import os
import sys

# 保证直接运行本文件（python gui/main_window.py）时能找到项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QGroupBox, QLabel, QMainWindow, QPlainTextEdit,
                             QProgressBar, QSplitter, QTabWidget, QVBoxLayout,
                             QWidget)

from gui.common import GuiLogHandler, LogBus, apply_style
from gui.pages import (BacktestPage, BatchPage, DataPage, OptimizePage,
                       SettingsPage)


class MainWindow(QMainWindow):
    """桌面版主窗口：单股回测 / 参数寻优 / 批量回测 / 数据下载 / 设置与关于"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("A股量化回测 —— 桌面版")
        self.resize(1280, 860)

        # 日志桥：root logger 加一个 handler，所有模块的 logging 都进日志框
        self.log_bus = LogBus()
        self._log_handler = GuiLogHandler(self.log_bus.message)
        logging.getLogger().addHandler(self._log_handler)
        self.log_bus.message.connect(self._append_log)

        # 五个标签页
        self.tabs = QTabWidget()
        self.backtest_page = BacktestPage(self)
        self.optimize_page = OptimizePage(self)
        self.batch_page = BatchPage(self)
        self.data_page = DataPage(self)
        self.settings_page = SettingsPage(self)
        self.tabs.addTab(self.backtest_page, "📈 单股回测")
        self.tabs.addTab(self.optimize_page, "🔍 参数寻优")
        self.tabs.addTab(self.batch_page, "🗂 批量回测")
        self.tabs.addTab(self.data_page, "📥 数据下载")
        self.tabs.addTab(self.settings_page, "⚙️ 设置与关于")

        # 底部日志框
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(5000)
        log_box_group = QGroupBox("运行日志")
        log_layout = QVBoxLayout(log_box_group)
        log_layout.addWidget(self.log_box)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.tabs)
        splitter.addWidget(log_box_group)
        splitter.setSizes([640, 200])

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(splitter)
        self.setCentralWidget(central)

        # 状态栏：进度文字 + 进度条
        self.progress_label = QLabel("")
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedWidth(280)
        self.progress_bar.hide()
        self.statusBar().addPermanentWidget(self.progress_label)
        self.statusBar().addPermanentWidget(self.progress_bar)
        self.statusBar().showMessage("就绪")

        self.log("桌面版已启动。在标签页中设置参数并点击「运行」，任务在后台线程执行，界面不会卡顿。")

    # ---------- 日志 / 进度（供页面调用） ----------

    def log(self, text: str):
        """追加一行到日志框（任何线程调用都安全）"""
        self._append_log(text)

    def _append_log(self, text: str):
        self.log_box.appendPlainText(text)
        bar = self.log_box.verticalScrollBar()
        bar.setValue(bar.maximum())

    def set_progress(self, pct: int, text: str = ""):
        """更新进度条。pct 0-100；-1 = 不确定进度（忙碌动画）"""
        if pct < 0:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(pct)
        self.progress_label.setText(text)
        self.progress_bar.show()

    def finish_progress(self, text: str = "完成"):
        """任务结束：进度拉满，3 秒后自动隐藏"""
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.progress_label.setText(text)
        QTimer.singleShot(3000, self.progress_bar.hide)


def run_test():
    """无界面冒烟测试（python gui/main_window.py --test）

    验证：主窗口 5 个标签页、图表绘制、表格渲染/排序/复制、
    页面结果展示、子线程任务往返。不联网、不跑真实回测。
    """
    import numpy as np
    import pandas as pd
    import pyqtgraph as pg
    from PyQt6.QtCore import QEventLoop
    from PyQt6.QtWidgets import QApplication

    from gui.common import DataFrameTable, plot_drawdown, plot_equity
    from gui.worker_thread import WorkerThread

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication(sys.argv)
    apply_style(app)
    win = MainWindow()

    # 1. 五个标签页
    assert win.tabs.count() == 5, f"标签页数量不对：{win.tabs.count()}"
    titles = [win.tabs.tabText(i) for i in range(win.tabs.count())]
    print("标签页：", " | ".join(titles))
    assert "单股回测" in titles[0] and "参数寻优" in titles[1]

    # 1.5 新策略出现在策略下拉框，且推送按钮初始为禁用
    names = [win.backtest_page.strategy_combo.itemText(i)
             for i in range(win.backtest_page.strategy_combo.count())]
    assert "动量策略" in names and "海龟策略" in names, f"下拉框缺少新策略：{names}"
    assert not win.backtest_page.push_btn.isEnabled(), "未有结果时推送按钮应禁用"
    assert not win.batch_page.push_btn.isEnabled(), "未有结果时批量推送按钮应禁用"
    print("策略下拉框：动量/海龟已出现；推送按钮初始禁用 ✓")

    # 1.6 四个页面都有「数据频率」下拉框（日线 + 5 档分钟线）+ 分钟提示联动
    for name, page in (("回测页", win.backtest_page), ("寻优页", win.optimize_page),
                       ("批量页", win.batch_page), ("数据页", win.data_page)):
        combo = page.freq_combo
        assert combo.count() == 6, f"{name}频率下拉框应有 6 项：{combo.count()}"
        assert combo.itemData(0) == "daily", f"{name}默认应为日线"
        assert combo.itemData(1) == "1min", f"{name}第二项应为 1 分钟"
        combo.setCurrentIndex(1)
        assert not page.minute_hint.isHidden(), f"{name}选分钟线时提示应显示"
        combo.setCurrentIndex(0)
        assert page.minute_hint.isHidden(), f"{name}切回日线时提示应隐藏"
    print("频率切换：4 页下拉框（日线 + 1/5/15/30/60 分钟）+ 提示联动 ✓")

    # 2. 图表：合成净值 / 回撤数据渲染
    idx = pd.date_range("2024-01-01", periods=200, freq="D")
    equity = pd.Series(np.linspace(1e6, 1.25e6, 200), index=idx)
    benchmark = pd.Series(np.linspace(1e6, 1.1e6, 200), index=idx)
    pw = pg.PlotWidget()
    plot_equity(pw, equity, benchmark)
    assert len(pw.getPlotItem().listDataItems()) == 2, "净值曲线应有两根线"
    dd = equity / equity.cummax() - 1
    pw2 = pg.PlotWidget()
    plot_drawdown(pw2, dd)
    print("图表绘制：净值 2 根线 + 回撤曲线 ✓")

    # 3. 表格：渲染 / 排序 / 复制
    table = DataFrameTable()
    table.set_dataframe(pd.DataFrame({
        "股票代码": ["600519", "000001"],
        "夏普比率": [1.2, -0.5],
        "最大回撤": [0.15, 0.20],
    }))
    assert table.rowCount() == 2 and table.columnCount() == 3
    assert table.item(0, 1).text() == "1.2000", table.item(0, 1).text()
    assert table.item(0, 2).text() == "15.00%", table.item(0, 2).text()
    table.sortByColumn(1, Qt.SortOrder.DescendingOrder)
    sorted_df = table._sorted_df()
    assert float(sorted_df["夏普比率"].iloc[0]) == 1.2, "数值排序应降序"
    table.selectAll()
    table.copy_selection()
    print("表格：渲染 / 数值排序 / 复制 ✓")

    # 4. 回测页结果展示（模拟数据，不跑真实回测）
    fake = {
        "code": "600519",
        "equity": equity,
        "benchmark": benchmark,
        "drawdown": dd,
        "metrics": {"累计收益率": 0.25, "年化收益率": 0.08, "最大回撤": 0.15,
                    "夏普比率": 0.9, "胜率": 0.55, "总交易次数": 12,
                    "总手续费": 1234.5, "期末总资产": 1_250_000, "基准收益率": 0.10},
        "risk_metrics": {"年化波动率": 0.18, "卡玛比率": 0.53, "盈亏比": 1.4},
        "trades": pd.DataFrame({
            "买入日期": ["2024-01-05", "2024-03-01"],
            "卖出日期": ["2024-02-02", "2024-04-10"],
            "买入价": [1600.0, 1700.0],
            "卖出价": [1650.0, 1680.0],
            "数量（股）": [100, 100],
            "盈亏（元）": [5000.0, -2000.0],
            "收益率": [0.031, -0.012],
            "状态": ["已平仓", "已平仓"],
        }),
        "yearly": {2024: 0.08},
    }
    # _last_info 由 _on_run 填写，测试里直接模拟一次（代码/策略/区间/资金）
    win.backtest_page._last_info = {"code": "600519", "策略": "双均线策略",
                                    "start": "2024-01-01", "end": "2024-12-31",
                                    "init_cash": 1_000_000}
    win.backtest_page._show_result(fake)
    assert win.backtest_page.export_btn.isEnabled()
    assert win.backtest_page.push_btn.isEnabled(), "有结果后推送按钮应可用"
    assert len(win.backtest_page.equity_plot.getPlotItem().listDataItems()) == 2
    print("回测页结果展示：图表 + 指标 + 交易明细 ✓")

    # 4.5 推送摘要：格式化的百分比/金额文本
    summary = win.backtest_page._build_summary()
    assert summary["股票代码"] == "600519", summary
    assert summary["累计收益率"] == "25.00%", summary
    assert summary["总交易次数"] == "12", summary
    assert summary["期末总资产"] == "1,250,000.00", summary
    assert "回测区间" in summary and "初始资金" in summary
    print("推送摘要：百分比/金额格式化正确 ✓")

    # 4.6 批量页推送摘要（模拟结果，不跑真实批量回测）
    win.batch_page._show_result({
        "results": pd.DataFrame([
            {"股票代码": "600519", "策略名称": "双均线策略", "夏普比率": 1.2, "累计收益率": 0.3},
            {"股票代码": "000001", "策略名称": "双均线策略", "夏普比率": -0.4, "累计收益率": -0.1},
        ]),
        "failed": ["600000 数据下载失败"],
        "elapsed": 12.3,
    })
    assert win.batch_page.push_btn.isEnabled(), "批量回测有结果后推送按钮应可用"
    bsum = win.batch_page._build_summary()
    assert bsum["最优组合（按夏普比率）"].startswith("600519"), bsum
    assert bsum["失败组合数"] == "1 个" and bsum["总用时"] == "12.3 秒", bsum
    print("批量页推送摘要：最优组合/失败数/用时正确 ✓")

    # 4.7 寻优页「用最优参数回测」：一键填参 + 切页 + 自动运行
    from unittest import mock

    from PyQt6.QtCore import QDate
    from PyQt6.QtWidgets import QMessageBox

    op = win.optimize_page
    op.code_edit.setText("600519")
    op.start_edit.setDate(QDate(2024, 1, 1))
    op.end_edit.setDate(QDate(2024, 12, 31))
    op.freq_combo.setCurrentIndex(op.freq_combo.findData("5min"))
    fake_out = {
        "best_params": {"fast": 5, "slow": 20},
        "metric": "夏普比率", "best_value": 1.23,
        "results": pd.DataFrame([{"fast": 5, "slow": 20, "夏普比率": 1.23}]),
        "failed": [],
        "best_metrics": {"累计收益率": 0.25, "年化收益率": 0.08, "最大回撤": 0.15,
                         "夏普比率": 1.23, "胜率": 0.55, "总交易次数": 12,
                         "总手续费": 123.4, "期末总资产": 1_250_000, "基准收益率": 0.10},
    }
    op._show_result(fake_out)
    assert op.apply_btn.isEnabled(), "寻优成功后一键应用按钮应可用"
    op.init_cash_spin.setValue(500_000)
    op.commission_spin.setValue(0.0002)
    op.slippage_spin.setValue(0.001)
    op.rule_t1.setChecked(False)
    with mock.patch.object(win.backtest_page, "_on_run") as m_run:
        op._apply_best()
    bp = win.backtest_page
    assert win.tabs.currentWidget() is bp, "应自动切到单股回测页"
    assert bp.code_edit.text() == "600519"
    assert bp.start_edit.date() == QDate(2024, 1, 1)
    assert bp.freq_combo.currentData() == "5min", "频率应同步为寻优时的 5 分钟线"
    assert bp._param_widgets["fast"].value() == 5, "最优参数 fast 未填到回测页"
    assert bp._param_widgets["slow"].value() == 20, "最优参数 slow 未填到回测页"
    assert bp.init_cash_spin.value() == 500_000
    assert bp.commission_spin.value() == 0.0002
    assert bp.slippage_spin.value() == 0.001
    assert not bp.rule_t1.isChecked(), "A股规则开关应同步"
    m_run.assert_called_once(), "应用后应自动开始回测"
    print("寻优页一键应用：参数/频率/规则同步 + 自动切页运行 ✓")

    # 4.8 寻优全部失败：友好弹窗、不崩溃、一键应用保持禁用
    with mock.patch.object(QMessageBox, "warning") as m_warn:
        op._show_result({"best_params": {}, "results": pd.DataFrame(),
                         "failed": ["fast=5,slow=5 回测失败"],
                         "metric": "夏普比率", "best_value": None})
        m_warn.assert_called_once()
    assert not op.apply_btn.isEnabled(), "寻优失败时一键应用应禁用"
    assert "寻优失败" in op.best_label.text()
    op.best_table.set_dataframe(None)  # 空表渲染不崩溃
    assert op.best_table.rowCount() == 0
    print("寻优失败分支：友好弹窗 + 空表渲染不崩溃 + 按钮禁用 ✓")

    # 5. 子线程任务往返（含 progress_cb）
    loop = QEventLoop()
    results, progress = {}, []

    def task(progress_cb=None):
        for i in range(3):
            progress_cb(i + 1, 3)
        return {"sum": 42}

    thread = WorkerThread(
        task, progress_cb=lambda done, total: progress.append((done, total)))
    thread.signals.finished.connect(lambda r: (results.update(r), loop.quit()))
    thread.signals.failed.connect(lambda e: (print("任务失败：", e), loop.quit()))
    thread.start()
    QTimer.singleShot(15000, loop.quit)  # 15 秒超时保护
    loop.exec()
    assert results.get("sum") == 42, f"子线程任务结果不对：{results}"
    assert progress == [(1, 3), (2, 3), (3, 3)], f"进度回调记录不对：{progress}"
    thread.wait(5000)
    print("子线程任务往返 + progress_cb ✓")

    # 6. 推送链路：mock 掉 Notifier，验证任务函数/结果弹窗，绝不真实联网
    from unittest import mock

    from PyQt6.QtWidgets import QMessageBox

    from gui.common import notify_config_status, push_result_task, report_push_result

    with mock.patch("notification.notifier.Notifier") as MockNotifier:
        MockNotifier.return_value.send_backtest_finished.return_value = \
            {"邮件": True, "企业微信": True}
        r = push_result_task({"任务类型": "单股回测", "股票代码": "600519"})
        assert r == {"邮件": True, "企业微信": True}
        sent = MockNotifier.return_value.send_backtest_finished.call_args[0][0]
        assert sent["股票代码"] == "600519"
    with mock.patch("notification.notifier.Notifier") as MockNotifier:
        MockNotifier.return_value.send_backtest_finished.return_value = {}
        assert push_result_task({}) == {}  # 渠道全关：返回空字典
    with mock.patch.object(QMessageBox, "information") as m_info, \
            mock.patch.object(QMessageBox, "warning") as m_warn:
        report_push_result(win, {}, win.log)
        m_info.assert_called_once()
        m_warn.assert_not_called()
        report_push_result(win, {"邮件": True, "企业微信": "未配置"}, win.log)
        m_warn.assert_called_once()  # 部分失败 → 警告弹窗
    status = notify_config_status()
    assert set(status) == {"email", "wecom"}
    assert all(isinstance(v, tuple) and len(v) == 2 for v in status.values())
    print("推送链路：任务函数 mock / 空渠道与部分失败弹窗 / 配置状态读取 ✓")

    win.close()
    print("===== GUI 冒烟测试全部通过 =====")


if __name__ == "__main__":
    if "--test" in sys.argv:
        run_test()
    else:
        from PyQt6.QtWidgets import QApplication
        app = QApplication(sys.argv)
        apply_style(app)
        window = MainWindow()
        window.show()
        sys.exit(app.exec())
