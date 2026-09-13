"""数据下载页：下载单只股票日线行情并预览"""

import os
import sys

from PyQt6.QtCore import QDate, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (QDateEdit, QFormLayout, QGroupBox, QHBoxLayout,
                             QLabel, QLineEdit, QPushButton, QSplitter,
                             QVBoxLayout, QWidget)

# 保证直接运行本文件时能找到项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from gui.common import (COLOR_ORANGE, MINUTE_HINT, AsyncTask, DataFrameTable,
                        make_freq_combo)


def _download_data(code, start, end, freq):
    """后台任务：下载行情数据（akshare 延迟导入，失败会由线程报错）"""
    from utils.data_loader import load_market_data
    df = load_market_data(code, start, end, freq=freq)
    return {"code": code, "df": df, "freq": freq}


class DataPage(QWidget):
    """数据下载标签页"""

    progress_changed = pyqtSignal(int, str)

    def __init__(self, win):
        super().__init__()
        self._win = win
        self._task = None
        self._last_df = None
        self._last_code = ""
        self._build_ui()
        self.progress_changed.connect(win.set_progress)

    # ---------- 界面搭建 ----------

    def _build_ui(self):
        root = QVBoxLayout(self)

        settings = QGroupBox("下载设置")
        form = QFormLayout(settings)
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("如 600519")
        self.start_edit = QDateEdit(QDate.currentDate().addYears(-3))
        self.end_edit = QDateEdit(QDate.currentDate())
        for e in (self.start_edit, self.end_edit):
            e.setCalendarPopup(True)
            e.setDisplayFormat("yyyy-MM-dd")
        form.addRow("股票代码", self.code_edit)
        form.addRow("开始日期", self.start_edit)
        form.addRow("结束日期", self.end_edit)
        self.freq_combo = make_freq_combo()
        form.addRow("数据频率", self.freq_combo)

        self.download_btn = QPushButton("▶ 下载行情")
        self.download_btn.clicked.connect(self._on_download)
        self.cache_btn = QPushButton("打开缓存目录")
        self.cache_btn.setObjectName("secondary")
        self.cache_btn.clicked.connect(self._open_cache_dir)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.download_btn, 1)
        btn_row.addWidget(self.cache_btn)

        hint = QLabel("数据源：日线走腾讯行情，分钟线走新浪行情（akshare），"
                      "下载后自动缓存到本地，再次下载同区间会直接读缓存。")
        hint.setWordWrap(True)
        hint.setObjectName("hint")

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
        left.addWidget(hint)
        left.addWidget(self.minute_hint)
        left.addLayout(btn_row)
        left.addStretch(1)
        left_widget = QWidget()
        left_widget.setLayout(left)
        left_widget.setMaximumWidth(430)

        # 右侧：信息 + 数据预览
        self.info_label = QLabel("尚未下载数据")
        self.info_label.setObjectName("hint")
        self.preview_table = DataFrameTable()
        preview_box = QGroupBox("行情数据预览（前 100 行，右键可导出）")
        p_layout = QVBoxLayout(preview_box)
        p_layout.addWidget(self.preview_table)

        right = QVBoxLayout()
        right.addWidget(self.info_label)
        right.addWidget(preview_box, 1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left_widget)
        right_widget = QWidget()
        right_widget.setLayout(right)
        splitter.addWidget(right_widget)
        splitter.setSizes([400, 980])
        root.addWidget(splitter)

    # ---------- 任务控制 ----------

    def set_running(self, running: bool):
        self.download_btn.setEnabled(not running)
        self.download_btn.setText("⏳ 下载中…" if running else "▶ 下载行情")
        if running:
            self.progress_changed.emit(-1, "正在下载行情数据…")

    def _on_download(self):
        if self._task is not None and self._task.thread.isRunning():
            return
        code = self.code_edit.text().strip()
        if not code:
            self._win.log("[提示] 请先填写股票代码")
            return
        freq = self.freq_combo.currentData()
        if freq != "daily":
            self._win.log(f"[提示] {MINUTE_HINT}")
        self._win.log(f"开始下载 {code} {self.freq_combo.currentText()}行情数据（"
                      f"{self.start_edit.date().toString('yyyy-MM-dd')} ~ "
                      f"{self.end_edit.date().toString('yyyy-MM-dd')}）")
        self._task = AsyncTask(
            self, _download_data,
            code, self.start_edit.date().toPyDate(), self.end_edit.date().toPyDate(),
            freq, on_finished=self._show_result)
        self._task.start()

    # ---------- 结果展示 ----------

    def _show_result(self, result):
        self._last_df = result["df"]
        self._last_code = result["code"]
        self._last_freq = result["freq"]
        df = result["df"]
        if df.empty:
            self.info_label.setText(f"{result['code']}：所选区间内没有数据")
            self.preview_table.set_dataframe(None)
            self._win.log(f"[提示] {result['code']} 所选区间内没有数据")
            self._win.finish_progress("数据下载完成（区间内无数据）")
            return
        last = df.iloc[-1]
        if self._last_freq == "daily":
            span = (f"{df['date'].iloc[0]:%Y-%m-%d} ~ "
                    f"{df['date'].iloc[-1]:%Y-%m-%d}")
            unit = "个交易日"
        else:
            span = (f"{df['date'].iloc[0]:%Y-%m-%d %H:%M} ~ "
                    f"{df['date'].iloc[-1]:%Y-%m-%d %H:%M}")
            unit = "根 K 线"
        self.info_label.setText(
            f"{result['code']}：共 {len(df)} {unit}，{span}，"
            f"最新收盘价 {last['close']:.2f} 元")
        freq_label = "日线" if self._last_freq == "daily" else "分钟线"
        self.preview_table.set_export_name(f"{result['code']}_{freq_label}行情")
        self.preview_table.set_dataframe(df.head(100))
        self._win.log(f"{result['code']} 行情下载完成：{len(df)} {unit}")
        self._win.finish_progress("数据下载完成")

    def _open_cache_dir(self):
        from utils.data_loader import CACHE_DIR
        os.makedirs(CACHE_DIR, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(CACHE_DIR))
