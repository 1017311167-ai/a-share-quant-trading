"""消息推送包 —— 邮件 + 企业微信机器人

用法:
    from notification import Notifier

    notifier = Notifier()   # 自动从项目根目录 .env 读取配置
    notifier.send_trade_signal({"股票代码": "600519", "信号": "买入", "价格": 1400.0})

推送失败只记录日志，不会中断主程序。
"""

import os
import sys

# 保证直接运行本目录下文件时能找到项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notification.notifier import Notifier, load_config

__all__ = ["Notifier", "load_config"]
