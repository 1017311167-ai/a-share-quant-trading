"""
A股量化回测软件 —— 启动入口

用法：
    python main.py            # 启动 Streamlit 网页版（浏览器自动打开）
    python main.py --gui      # 启动 PyQt6 桌面版
"""

import argparse
import os
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description="A股量化回测软件")
    parser.add_argument("--gui", action="store_true",
                        help="启动 PyQt6 桌面版界面（默认启动网页版）")
    args = parser.parse_args()

    if args.gui:
        from gui import run_gui
        sys.exit(run_gui())

    # 默认：Streamlit 网页版，app/streamlit_app.py 是本软件的图形界面
    app_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "streamlit_app.py")

    print("正在启动 A股量化回测软件（网页版） ...")
    print("提示：想用桌面版界面请运行 python main.py --gui")
    print("启动成功后浏览器会自动打开界面，按 Ctrl+C 可退出。")
    print()

    # 用当前 Python 解释器启动 streamlit，避免环境变量问题
    subprocess.run([sys.executable, "-m", "streamlit", "run", app_path])


if __name__ == "__main__":
    main()
