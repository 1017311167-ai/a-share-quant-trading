"""桌面版启动脚本（也是 PyInstaller 打包入口）

用法:
    python gui/launcher.py

打包成单文件 exe（在 Windows 上执行）:
    pip install pyinstaller
    pyinstaller --onefile --windowed --name A股量化回测 gui/launcher.py
"""

import os
import sys

# 打包后模块自动收集，此处保证开发环境直接运行也能找到项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gui import run_gui

if __name__ == "__main__":
    sys.exit(run_gui())
