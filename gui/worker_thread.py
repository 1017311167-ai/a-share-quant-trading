"""回测工作线程：所有回测 / 优化 / 数据下载任务都放到子线程执行，界面不卡顿

约定：
    - 任务函数 fn(*args, **kwargs) 返回结果对象（由 finished 信号带出来）
    - 任务函数可选参数 progress_cb(已完成数, 总数)，用于报告进度
    - Qt 的信号跨线程安全：子线程里 emit，主线程自动排队接收，
      所以结果处理函数里可以直接更新界面
"""

import os
import sys
import traceback

# 保证直接运行本目录下文件时能找到项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QObject, QThread, pyqtSignal


class WorkerSignals(QObject):
    """工作线程的信号集合"""
    progress = pyqtSignal(int, str)   # (进度百分比, 描述文字)；-1 = 不确定进度
    finished = pyqtSignal(object)     # 任务返回值
    failed = pyqtSignal(str)          # 失败原因（含最后一行错误位置）


class WorkerThread(QThread):
    """通用后台任务线程：run() 里执行 fn(*args, **kwargs)

    用法:
        thread = WorkerThread(某任务函数, 参数1, 参数2, progress_cb=回调)
        thread.signals.finished.connect(界面更新函数)
        thread.signals.failed.connect(报错处理函数)
        thread.start()

    注意：保存 thread 的引用直到任务结束，防止线程对象被垃圾回收。
    """

    def __init__(self, fn, *args, parent=None, **kwargs):
        super().__init__(parent)
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.signals = WorkerSignals()
        # macOS 上 QThread 默认线程栈只有 512KB，numba/vectorbt 编译代码会栈溢出
        # 直接崩溃（SIGILL）；统一放大到 32MB（Windows 上该设置被忽略，无副作用）
        self.setStackSize(32 * 1024 * 1024)

    def run(self):
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as e:
            tb = traceback.format_exc()
            last_line = tb.strip().splitlines()[-1] if tb.strip() else ""
            self.signals.failed.emit(f"{type(e).__name__}: {e}\n{last_line}")
        else:
            self.signals.finished.emit(result)
