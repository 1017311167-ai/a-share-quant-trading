"""PyQt6 桌面版界面包

用法:
    from gui import run_gui
    run_gui()

命令行:
    python main.py --gui        # 桌面版
    python gui/launcher.py      # 桌面版（也是 pyinstaller 打包入口）
"""

import os
import sys

# 保证任何方式导入 gui 包都能找到项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run_gui() -> int:
    """启动桌面版界面（QApplication + 主窗口），返回应用退出码"""
    from PyQt6.QtWidgets import QApplication

    from gui.common import apply_style
    from gui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    apply_style(app)
    window = MainWindow()
    window.show()
    return app.exec()


__all__ = ["run_gui"]
