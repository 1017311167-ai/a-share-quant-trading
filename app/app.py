"""图形界面（旧入口，保留兼容）

实际界面代码已迁移到 app/streamlit_app.py，
本文件只负责转发，两种启动方式等效：
    streamlit run app/app.py
    streamlit run app/streamlit_app.py
"""

import os

_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "streamlit_app.py")
exec(compile(open(_path, encoding="utf-8").read(), _path, "exec"))
