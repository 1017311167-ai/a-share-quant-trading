"""设置与关于页：回测默认值（自动记住）、消息推送状态与测试、项目说明"""

import os
import sys

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
                             QLabel, QMessageBox, QPushButton, QSpinBox,
                             QVBoxLayout, QWidget)

# 保证直接运行本文件时能找到项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from gui.common import (AsyncTask, load_defaults, notify_config_status,
                        report_push_result, save_default)

_ABOUT_TEXT = """
<h3>A股量化回测软件</h3>
<p>Python + VectorBT + AKShare + PyQt6 编写的桌面量化回测工具。</p>
<p><b>启动方式</b><br>
&nbsp;&nbsp;• 网页版：<code>python main.py</code>（浏览器界面）<br>
&nbsp;&nbsp;• 桌面版：<code>python main.py --gui</code>（本界面）</p>
<p><b>打包成 exe</b>（Windows，需安装 pyinstaller）：<br>
&nbsp;&nbsp;<code>pyinstaller --onefile --windowed --name A股量化回测 gui/launcher.py</code></p>
<p><b>消息推送配置</b>：复制项目根目录 .env.example 为 .env，填入邮箱 SMTP 授权码 /
企业微信机器人 Webhook 后即可使用（.env 已被 .gitignore 忽略，密钥不会泄露）。</p>
<p style='color:#898781;'>本软件仅用于学习研究，不构成投资建议。</p>
"""


def _send_test_email():
    """后台任务：发一封测试邮件（真实配置来自 .env，未配置会返回错误信息）"""
    from notification.notifier import Notifier
    notifier = Notifier(channels=["email"])
    return notifier.send(
        "【测试】A股量化回测软件",
        "这是一封测试邮件，收到说明邮件推送配置成功。",
        html_body="<h3>A股量化回测软件</h3><p>这是一封<b>测试邮件</b>，"
                  "收到说明邮件推送配置成功。</p>",
        channels=["email"])


def _send_test_wecom():
    """后台任务：发一条企业微信机器人测试消息（text 类型）"""
    from notification.notifier import Notifier
    notifier = Notifier(channels=["wecom"])
    return notifier.send(
        "【测试】A股量化回测软件：这是一条企业微信机器人测试消息（text 类型）",
        channels=["wecom"])


class SettingsPage(QWidget):
    """设置与关于标签页"""

    progress_changed = pyqtSignal(int, str)

    def __init__(self, win):
        super().__init__()
        self._win = win
        self._task = None
        self._build_ui()
        self.progress_changed.connect(win.set_progress)

    # ---------- 界面搭建 ----------

    def _build_ui(self):
        root = QVBoxLayout(self)

        # 回测默认值
        d = load_defaults()
        defaults_box = QGroupBox("回测默认值（修改后自动保存，各页面下次启动沿用）")
        form = QFormLayout(defaults_box)
        self.init_cash_spin = QDoubleSpinBox()
        self.init_cash_spin.setRange(10_000, 1e12)
        self.init_cash_spin.setDecimals(0)
        self.init_cash_spin.setGroupSeparatorShown(True)
        self.init_cash_spin.blockSignals(True)
        self.init_cash_spin.setValue(d["init_cash"])
        self.init_cash_spin.blockSignals(False)
        self.commission_spin = QDoubleSpinBox()
        self.commission_spin.setRange(0.0, 0.1)
        self.commission_spin.setDecimals(5)
        self.commission_spin.setSingleStep(0.00005)
        self.commission_spin.blockSignals(True)
        self.commission_spin.setValue(d["commission"])
        self.commission_spin.blockSignals(False)
        self.slippage_spin = QDoubleSpinBox()
        self.slippage_spin.setRange(0.0, 0.1)
        self.slippage_spin.setDecimals(4)
        self.slippage_spin.setSingleStep(0.0005)
        self.slippage_spin.blockSignals(True)
        self.slippage_spin.setValue(d["slippage"])
        self.slippage_spin.blockSignals(False)
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 32)
        self.workers_spin.blockSignals(True)
        self.workers_spin.setValue(d["max_workers"])
        self.workers_spin.blockSignals(False)

        for key, w in (("init_cash", self.init_cash_spin), ("commission", self.commission_spin),
                       ("slippage", self.slippage_spin), ("max_workers", self.workers_spin)):
            w.editingFinished.connect(lambda k=key, widget=w: self._save_default(k, widget.value()))
        form.addRow("初始资金（元）", self.init_cash_spin)
        form.addRow("佣金费率", self.commission_spin)
        form.addRow("滑点", self.slippage_spin)
        form.addRow("并发进程数", self.workers_spin)

        # 消息推送状态（配置来自项目根目录 .env 文件；回测页/批量页的推送按钮共用此配置）
        notify_box = QGroupBox("消息推送（配置来自项目根目录 .env 文件）")
        n_layout = QVBoxLayout(notify_box)
        status = notify_config_status()
        self._email_ready, email_text = status["email"]
        self._wecom_ready, wecom_text = status["wecom"]
        n_layout.addWidget(QLabel(f"邮件通知：{email_text}"))
        n_layout.addWidget(QLabel(f"企业微信机器人：{wecom_text}"))

        self.email_btn = QPushButton("发送测试邮件")
        self.email_btn.clicked.connect(self._on_test_email)
        self.wecom_btn = QPushButton("发送企业微信测试消息")
        self.wecom_btn.setObjectName("secondary")
        self.wecom_btn.clicked.connect(self._on_test_wecom)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.email_btn)
        btn_row.addWidget(self.wecom_btn)
        n_layout.addLayout(btn_row)

        # 关于
        about_box = QGroupBox("关于")
        a_layout = QVBoxLayout(about_box)
        about_label = QLabel(_ABOUT_TEXT)
        about_label.setTextFormat(Qt.TextFormat.RichText)
        about_label.setOpenExternalLinks(True)
        about_label.setWordWrap(True)
        a_layout.addWidget(about_label)

        root.addWidget(defaults_box)
        root.addWidget(notify_box)
        root.addWidget(about_box)
        root.addStretch(1)

    # ---------- 默认值保存 ----------

    def _save_default(self, key, value):
        save_default(key, value)
        self._win.log(f"已保存默认值：{key} = {value}")

    # ---------- 测试推送 ----------

    def set_running(self, running: bool):
        self.email_btn.setEnabled(not running)
        self.wecom_btn.setEnabled(not running)
        if running:
            self.progress_changed.emit(-1, "正在发送测试消息…")

    def _on_test_email(self):
        if self._task is not None and self._task.thread.isRunning():
            return
        if not self._email_ready:
            QMessageBox.information(
                self, "提示", "邮件渠道未配置。\n请复制项目根目录 .env.example 为 .env，"
                              "填写 SMTP_HOST / SMTP_USER / SMTP_PASSWORD（授权码）/ MAIL_TO。")
            return
        self._win.log("发送测试邮件…（请勿在测试中重复点击，真实发送需要几秒）")
        self._task = AsyncTask(self, _send_test_email, on_finished=self._show_result)
        self._task.start()

    def _on_test_wecom(self):
        if self._task is not None and self._task.thread.isRunning():
            return
        if not self._wecom_ready:
            QMessageBox.information(
                self, "提示", "企业微信渠道未配置。\n请复制项目根目录 .env.example 为 .env，"
                              "填写 WECOM_WEBHOOK（企业微信群机器人地址）。")
            return
        self._win.log("发送企业微信测试消息…")
        self._task = AsyncTask(self, _send_test_wecom, on_finished=self._show_result)
        self._task.start()

    def _show_result(self, result):
        """result = {渠道名: True 或 错误信息}（与回测页/批量页共用同一处理逻辑）"""
        self._win.finish_progress("推送完成")
        report_push_result(self, result, self._win.log)
