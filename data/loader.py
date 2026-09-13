"""
行情数据加载

实际实现位于 utils/data_loader.py，这里保留同名函数，方便按目录职责调用：
    from data.loader import load_daily_data, load_minute_data, load_market_data
"""

from utils.data_loader import (load_daily_data, load_market_data,  # noqa: F401
                               load_minute_data)
