"""GUI 共用组件：样式、结果表格、图表绘制、日志桥接、后台任务封装

本模块只依赖 PyQt6 / pyqtgraph / pandas / numpy（轻量），
vectorbt、akshare 等重型库一律在页面任务函数里延迟导入，保证界面秒开。
"""

import logging
import math
import os
import sys

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QObject, QSettings, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QFileDialog, QMenu,
                             QTableWidget, QTableWidgetItem)

# 保证直接运行本目录下文件（python gui/main_window.py）时能找到项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gui.worker_thread import WorkerThread

# ---------- 配色（与网页版统一的项目配色） ----------
COLOR_SURFACE = "#fcfcfb"          # 背景
COLOR_BLUE = "#2a78d6"             # 策略净值
COLOR_ORANGE = "#eb6834"           # 基准（买入持有）
COLOR_BUY = "#d03b3b"              # 买入/亏损
COLOR_SELL = "#0ca30c"             # 卖出/盈利
COLOR_GRID = "#e1e0d9"             # 网格线
COLOR_INK = "#0b0b0b"              # 主文字
COLOR_INK_SECONDARY = "#52514e"    # 次要文字
COLOR_HINT = "#898781"             # 弱化文字

# 参数寻优可选的目标指标（与 optimization/_common.py 保持一致，避免界面启动时导入 vectorbt）
OPTIMIZE_METRICS = [
    "累计收益率", "年化收益率", "年化波动率", "夏普比率", "卡玛比率",
    "最大回撤", "胜率", "盈亏比", "总交易次数", "总手续费", "期末总资产", "基准收益率",
]

# 行情频率选项：(显示名, 取值)。各页面的「数据频率」下拉框统一用 make_freq_combo() 创建
FREQ_LABELS = [
    ("日线", "daily"),
    ("1 分钟", "1min"),
    ("5 分钟", "5min"),
    ("15 分钟", "15min"),
    ("30 分钟", "30min"),
    ("60 分钟", "60min"),
]

# 分钟线说明（回测/寻优/批量页选分钟线时显示）
MINUTE_HINT = ("分钟线数据来自新浪行情，只提供最近约 5 个交易日；"
               "T+1/涨跌停等规则按分钟线逐根顺延（近似处理）。")

# ---------- 全局样式 ----------

STYLE_SHEET = f"""
QTabWidget::pane {{ border: 1px solid {COLOR_GRID}; background: {COLOR_SURFACE}; }}
QTabBar::tab {{
    padding: 8px 20px; background: #eeede8; border: 1px solid #d8d7cf;
    border-bottom: none; margin-right: 2px; color: {COLOR_INK_SECONDARY};
    border-top-left-radius: 6px; border-top-right-radius: 6px;
}}
QTabBar::tab:selected {{
    background: {COLOR_SURFACE}; color: {COLOR_INK}; font-weight: bold;
    border-bottom: 2px solid {COLOR_BLUE};
}}
QGroupBox {{
    font-weight: bold; border: 1px solid #d8d7cf; border-radius: 6px;
    margin-top: 12px; background: {COLOR_SURFACE};
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {COLOR_INK}; }}
QPushButton {{
    background: {COLOR_BLUE}; color: white; border: none; border-radius: 5px;
    padding: 7px 18px;
}}
QPushButton:hover {{ background: #3d88e2; }}
QPushButton:disabled {{ background: #c6c4ba; color: #f2f1ec; }}
QPushButton#secondary {{
    background: {COLOR_SURFACE}; color: {COLOR_INK}; border: 1px solid #c3c2b7;
}}
QPushButton#secondary:hover {{ background: #eeede8; }}
QLineEdit, QComboBox, QDateEdit, QSpinBox, QDoubleSpinBox {{
    border: 1px solid #c3c2b7; border-radius: 4px; padding: 4px 6px; background: #ffffff;
}}
QComboBox::drop-down {{ border: none; }}
QPlainTextEdit, QTextEdit {{
    border: 1px solid #c3c2b7; border-radius: 4px; background: {COLOR_SURFACE};
}}
QTableWidget {{ background: {COLOR_SURFACE}; gridline-color: {COLOR_GRID}; }}
QHeaderView::section {{
    background: #eeede8; border: none; border-right: 1px solid {COLOR_GRID};
    border-bottom: 1px solid {COLOR_GRID}; padding: 5px 8px;
    color: {COLOR_INK}; font-weight: bold;
}}
QProgressBar {{
    border: 1px solid #c3c2b7; border-radius: 4px; background: {COLOR_SURFACE};
    text-align: center; min-height: 16px;
}}
QProgressBar::chunk {{ background: {COLOR_BLUE}; border-radius: 3px; }}
QListWidget {{ background: #ffffff; border: 1px solid #c3c2b7; border-radius: 4px; }}
QListWidget::item {{ padding: 3px 6px; }}
QLabel#big {{ font-size: 16px; font-weight: bold; color: {COLOR_SELL}; }}
QLabel#hint {{ color: {COLOR_HINT}; }}
QSplitter::handle {{ background: {COLOR_GRID}; }}
"""


def apply_style(app):
    """全局样式：Fusion 风格 + 项目配色 + 中文字体（Qt6 自动适配高 DPI 缩放）"""
    app.setStyle("Fusion")
    font = QFont("Microsoft YaHei")   # Windows 中文界面字体；macOS 上自动回退
    font.setPointSize(10)
    app.setFont(font)
    app.setStyleSheet(STYLE_SHEET)


def make_freq_combo():
    """创建「数据频率」下拉框（日线 / 1~60 分钟线），data = 频率字符串"""
    from PyQt6.QtWidgets import QComboBox
    combo = QComboBox()
    for label, value in FREQ_LABELS:
        combo.addItem(label, value)
    return combo


# ---------- 设置（界面默认值持久化） ----------

_ORG = "ABacktest"


def load_defaults() -> dict:
    """读取回测默认参数（用户改过就会记住）"""
    s = QSettings(_ORG, _ORG)

    def get(key, default, cast):
        try:
            return cast(s.value(f"defaults/{key}", default))
        except Exception:
            return default

    return {
        "init_cash": get("init_cash", 1_000_000, float),
        "commission": get("commission", 0.00025, float),
        "slippage": get("slippage", 0.001, float),
        "max_workers": get("max_workers", 4, int),
    }


def save_default(key, value):
    """保存单个默认参数（供设置页调用）"""
    QSettings(_ORG, _ORG).setValue(f"defaults/{key}", value)


# ---------- 日志桥接：logging 记录自动显示到界面日志框 ----------

class LogBus(QObject):
    """日志信号中转站（信号跨线程安全，工作线程里打日志也能显示）"""
    message = pyqtSignal(str)


class GuiLogHandler(logging.Handler):
    """把 logging 记录转发到界面日志框"""

    def __init__(self, signal):
        super().__init__()
        self._signal = signal
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record):
        try:
            self._signal.emit(self.format(record))
        except Exception:
            pass  # 界面已关闭等极端情况，忽略


# ---------- 结果表格：排序 / 复制 / 导出 ----------

class SortItem(QTableWidgetItem):
    """带数值排序键的表格项：显示格式化文本，排序按原始数值

    Qt6 里 DisplayRole/EditRole 是同一个槽，setData 会把显示文本也覆盖掉，
    所以改用 __lt__（QTableWidget 排序走它）实现"显示好看、排序正确"。
    """

    def __init__(self, text, sort_value=None):
        super().__init__(text)
        self._sort_value = sort_value

    def __lt__(self, other):
        if self._sort_value is not None and isinstance(other, SortItem) \
                and other._sort_value is not None:
            return self._sort_value < other._sort_value
        return super().__lt__(other)


class DataFrameTable(QTableWidget):
    """DataFrame → 表格。支持：
    - 点击表头排序（数值按大小排，不按字符串）
    - Ctrl+C / 右键菜单复制选中内容
    - 右键导出 CSV / Excel
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._df = None
        self._export_name = "导出"
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.setSortingEnabled(True)
        self.horizontalHeader().setHighlightSections(False)
        self.verticalHeader().setVisible(False)

    def set_export_name(self, name):
        """导出文件时的默认文件名（不含扩展名）"""
        self._export_name = name

    def set_dataframe(self, df: pd.DataFrame):
        """整表刷新为 df 的内容；df 为 None / 空表时清空表格"""
        if df is None or df.empty:
            self._df = df
            self.setSortingEnabled(False)
            self.clear()
            self.setColumnCount(0)
            self.setRowCount(0)
            self.setSortingEnabled(True)
            return
        df = df.reset_index(drop=True)
        self._df = df
        self.setSortingEnabled(False)  # 填充期间关排序，避免重绘抖动
        self.clear()
        self.setColumnCount(len(df.columns))
        self.setHorizontalHeaderLabels([str(c) for c in df.columns])
        self.setRowCount(len(df))
        for r, row in enumerate(df.itertuples(index=False, name=None)):
            for c, v in enumerate(row):
                self.setItem(r, c, self._make_item(v, str(df.columns[c])))
        self.setSortingEnabled(True)
        self.resizeColumnsToContents()

    def _make_item(self, v, colname: str) -> QTableWidgetItem:
        """单元格：数值列用 SortItem 存原始数值作排序键，显示文本单独格式化"""
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return QTableWidgetItem("")
        if isinstance(v, bool):
            return QTableWidgetItem("是" if v else "否")
        if isinstance(v, pd.Timestamp):
            return QTableWidgetItem(v.strftime("%Y-%m-%d"))
        if isinstance(v, (int, np.integer)):
            item = SortItem(str(v), float(v))
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return item
        if isinstance(v, (float, np.floating)):
            is_percent = ("率" in colname and "比" not in colname) or colname == "最大回撤"
            if is_percent:
                text = f"{v * 100:.2f}%"          # 收益率/回撤类按百分比显示
            elif "价" in colname or "元" in colname:
                text = f"{v:.2f}"                 # 价格/金额保留两位
            else:
                text = f"{v:.4f}"                 # 比率类保留四位
            item = SortItem(text, float(v))
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return item
        return QTableWidgetItem(str(v))

    # ---- 复制 ----

    def keyPressEvent(self, event):
        from PyQt6.QtGui import QKeySequence
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_selection()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction("复制选中内容", self.copy_selection)
        menu.addSeparator()
        menu.addAction("导出 CSV…", self._export_csv)
        menu.addAction("导出 Excel…", self._export_excel)
        menu.exec(event.globalPos())

    def copy_selection(self):
        """把选中区域复制为制表符分隔文本（可直接粘贴到 Excel）"""
        cells = {}
        for rng in self.selectedRanges():
            for r in range(rng.topRow(), rng.bottomRow() + 1):
                for c in range(rng.leftColumn(), rng.rightColumn() + 1):
                    item = self.item(r, c)
                    if item is not None:
                        cells[(r, c)] = item
        if not cells:
            return
        rows = sorted({r for r, _ in cells})
        cols = sorted({c for _, c in cells})
        lines = []
        for r in rows:
            lines.append("\t".join(
                cells[(r, c)].text() if (r, c) in cells else "" for c in cols))
        QApplication.clipboard().setText("\n".join(lines))

    # ---- 导出 ----

    def export_csv_dialog(self):
        """弹窗选择路径并导出 CSV（公开方法，供页面按钮调用）"""
        self._export_csv()

    def export_excel_dialog(self):
        """弹窗选择路径并导出 Excel（公开方法，供页面按钮调用）"""
        self._export_excel()

    def _sorted_df(self) -> pd.DataFrame:
        """按当前表头排序状态重排底层 DataFrame（导出与表格显示顺序一致）"""
        df = self._df
        if df is None or df.empty:
            return df
        header = self.horizontalHeader()
        col, order = header.sortIndicatorSection(), header.sortIndicatorOrder()
        if 0 <= col < df.shape[1]:
            try:
                df = df.sort_values(df.columns[col],
                                    ascending=(order == Qt.SortOrder.AscendingOrder),
                                    kind="stable")
            except Exception:
                pass
        return df

    def _export_csv(self):
        df = self._sorted_df()
        if df is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 CSV", f"{self._export_name}.csv", "CSV 文件 (*.csv)")
        if path:
            df.to_csv(path, index=False, encoding="utf-8-sig")
            self._notify_log(f"已导出：{path}")

    def _export_excel(self):
        df = self._sorted_df()
        if df is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 Excel", f"{self._export_name}.xlsx", "Excel 文件 (*.xlsx)")
        if path:
            df.to_excel(path, index=False)
            self._notify_log(f"已导出：{path}")

    def _notify_log(self, text):
        """如果挂在主窗口上，把导出结果写进主窗口日志框"""
        win = self.window()
        if hasattr(win, "log"):
            win.log(text)


# ---------- 指标显示 ----------

def format_metric_value(name: str, v) -> str:
    """绩效指标值 → 显示字符串"""
    if v is None:
        return "—"
    if name == "总交易次数":
        return str(int(v))
    if name in ("总手续费", "期末总资产"):
        return f"{v:,.2f}"
    if name in ("累计收益率", "年化收益率", "最大回撤", "胜率", "基准收益率", "年化波动率"):
        return f"{v * 100:.2f}%"
    return f"{v:.4f}" if isinstance(v, float) else str(v)


def metrics_to_table(metrics: dict, risk_metrics: dict | None = None,
                     extra: dict | None = None) -> pd.DataFrame:
    """指标字典 → 两列展示表格（指标 | 数值）"""
    merged = {}
    if metrics:
        merged.update(metrics)
    if risk_metrics:
        merged.update(risk_metrics)
    if extra:
        merged.update(extra)
    order = ["累计收益率", "年化收益率", "年化波动率", "夏普比率", "卡玛比率",
             "最大回撤", "胜率", "盈亏比", "总交易次数", "总手续费",
             "期末总资产", "基准收益率"]
    keys = [k for k in order if k in merged] + [k for k in merged if k not in order]
    return pd.DataFrame({
        "指标": keys,
        "数值": [format_metric_value(k, merged[k]) for k in keys],
    })


# ---------- pyqtgraph 图表 ----------

def _setup_date_axis(plot_widget):
    """把底部轴换成日期轴（之后 x 数据传 Unix 秒）"""
    if not isinstance(plot_widget.getAxis("bottom"), pg.DateAxisItem):
        plot_widget.setAxisItems({"bottom": pg.DateAxisItem(orientation="bottom")})


def _to_unix_seconds(index) -> list:
    """DatetimeIndex → Unix 秒列表（DateAxisItem 的 x 坐标）"""
    try:
        ns = index.asi8
    except AttributeError:
        ns = pd.DatetimeIndex(index).asi8
    return (ns / 1e9).tolist()


def plot_equity(widget: pg.PlotWidget, equity: pd.Series, benchmark: pd.Series | None = None):
    """净值曲线：策略 vs 买入持有基准（数据点少、绘制快，不卡界面）"""
    widget.clear()
    _setup_date_axis(widget)
    x = _to_unix_seconds(equity.index)
    widget.plot(x, equity.values.tolist(),
                pen=pg.mkPen(COLOR_BLUE, width=2), name="策略净值")
    if benchmark is not None:
        widget.plot(x, benchmark.values.tolist(),
                    pen=pg.mkPen(COLOR_ORANGE, width=2, style=Qt.PenStyle.DashLine),
                    name="买入持有")
    widget.setBackground(COLOR_SURFACE)
    widget.showGrid(x=True, y=True, alpha=0.25)
    widget.setLabel("left", "账户资产（元）")
    widget.setLabel("bottom", "日期")
    widget.setClipToView(True)
    if benchmark is not None:
        widget.addLegend(offset=(10, 10), labelTextColor=COLOR_INK_SECONDARY)


def plot_drawdown(widget: pg.PlotWidget, drawdown: pd.Series):
    """回撤曲线（百分比，≤0），最大回撤点标数字"""
    widget.clear()
    _setup_date_axis(widget)
    x = _to_unix_seconds(drawdown.index)
    y = (drawdown.values * 100).tolist()
    widget.plot(x, y, pen=pg.mkPen(COLOR_SELL, width=2), fillLevel=0,
                brush=pg.mkBrush(12, 163, 12, 50))
    i_min = int(np.argmin(y))
    widget.plot([x[i_min]], [y[i_min]], pen=None, symbol="o", symbolSize=9,
                symbolBrush=pg.mkBrush(COLOR_SELL),
                symbolPen=pg.mkPen(COLOR_SURFACE, width=1))
    label = pg.TextItem(f"最大回撤 {y[i_min]:.2f}%", color=COLOR_SELL, anchor=(0.5, 1.5))
    label.setPos(x[i_min], y[i_min])
    widget.addItem(label)
    widget.setBackground(COLOR_SURFACE)
    widget.showGrid(x=True, y=True, alpha=0.25)
    widget.setLabel("left", "回撤（%）")
    widget.setLabel("bottom", "日期")
    widget.setClipToView(True)


# ---------- 后台任务封装 ----------

class AsyncTask(QObject):
    """页面后台任务：创建 WorkerThread 并管理生命周期

    用法:
        page._task = AsyncTask(page, fn, *args, on_finished=回调, **kwargs)
        page._task.start()

    任务函数约定：可选参数 progress_cb(已完成数, 总数) 报告进度。
    成功 → 回调在界面线程执行；失败 → 弹窗 + 日志。
    """

    def __init__(self, page, fn, *args, on_finished, on_failed=None,
                 manage_running=True, **kwargs):
        super().__init__(page)
        self._page = page
        self._on_finished = on_finished
        self._on_failed = on_failed
        self._manage_running = manage_running  # False：次要任务（如推送），不切换页面按钮状态
        self.thread = WorkerThread(fn, *args, **kwargs)
        self.thread.signals.finished.connect(self._done)
        self.thread.signals.failed.connect(self._fail)
        # 注意：不要对 thread 用 deleteLater —— 页面仍持有 self.thread 引用，
        # 立即删除 C++ 对象会让页面里的 isRunning() 抛 RuntimeError；
        # 等下次运行时页面用新 AsyncTask 替换本对象，旧的随垃圾回收销毁即可

    def start(self):
        if self._manage_running:
            self._page.set_running(True)
        self.thread.start()

    def _done(self, result):
        if self._manage_running:
            self._page.set_running(False)
        try:
            self._on_finished(result)
        except Exception as e:  # 结果处理出错只记日志，不让界面崩溃
            self._page.window().log(f"[结果处理失败] {type(e).__name__}: {e}")

    def _fail(self, err):
        if self._manage_running:
            self._page.set_running(False)
        self._page.window().log(f"[任务失败] {err.splitlines()[0]}")
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.warning(self._page, "任务失败", err)
        if self._on_failed:
            self._on_failed(err)


# ---------- 消息推送（回测完成后自动/手动推送，复用 notification 模块） ----------

def notify_config_status() -> dict:
    """读取 .env 推送配置状态，返回 {"email": (是否可用, 说明文字), "wecom": (...)}。
    只读配置、不联网。"""
    try:
        from notification.notifier import load_config
        cfg = load_config()
        email_cfg, wecom_cfg = cfg["email"], cfg["wecom"]
        email_ready = bool(email_cfg["enabled"] and email_cfg["host"]
                           and email_cfg["user"] and email_cfg["password"]
                           and email_cfg["to"])
        wecom_ready = bool(wecom_cfg["enabled"] and wecom_cfg["webhook"])
        email_text = ("已启用，收件人 " + email_cfg["to"]) if email_ready \
            else "未完成配置（请编辑 .env 填写 SMTP 信息并打开 NOTIFY_EMAIL=true）"
        wecom_text = "已启用" if wecom_ready \
            else "未完成配置（请编辑 .env 填写 WECOM_WEBHOOK 并打开 NOTIFY_WECOM=true）"
    except Exception as e:
        text = f"配置读取失败（{type(e).__name__}: {e}）"
        return {"email": (False, text), "wecom": (False, text)}
    return {"email": (email_ready, email_text), "wecom": (wecom_ready, wecom_text)}


def push_result_task(summary: dict) -> dict:
    """后台任务：把回测结果摘要推送出去（真实发送，渠道由 .env 开关决定）。
    返回 {渠道中文名: True 或 错误字符串}；渠道全关时返回空字典，绝不抛异常。"""
    from notification.notifier import Notifier
    return Notifier().send_backtest_finished(summary)


def report_push_result(parent, result, log):
    """统一处理推送结果（各页面共用）：弹窗提示 + 写日志。
    result = {渠道中文名: True 或 错误字符串}（空字典 = 没有可用渠道）"""
    from PyQt6.QtWidgets import QMessageBox
    if not result:
        QMessageBox.information(
            parent, "推送结果",
            "没有可用的推送渠道。\n请在 .env 中打开 NOTIFY_EMAIL 或 NOTIFY_WECOM 并填写配置。")
        log("[推送] 没有可用的推送渠道（.env 中渠道均未启用），未发送")
        return
    ok = [k for k, v in result.items() if v is True]
    bad = [(k, v) for k, v in result.items() if v is not True]
    if ok and not bad:
        QMessageBox.information(parent, "推送成功", "、".join(ok) + " 推送成功！")
        log("[推送] 成功：" + "、".join(ok))
    elif ok:
        detail = "\n".join(f"{k}：{v}" for k, v in bad)
        QMessageBox.warning(parent, "推送结果", f"部分渠道推送失败：\n{detail}")
        log(f"[推送] 部分失败：{detail}")
    else:
        detail = "\n".join(f"{k}：{v}" for k, v in bad)
        QMessageBox.warning(parent, "推送结果", f"推送失败：\n{detail}")
        log(f"[推送] 失败：{detail}")
