"""行情数据兼容入口。

实际实现已经迁移到 data 包。本模块保留原有函数名，避免破坏现有回测、策略、
参数寻优和 GUI 调用。
"""

import os
import sys

# 保证直接运行本文件时能找到项目根目录下的 data 包。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.trading_calendar import TradingCalendar  # noqa: F401
from data.models import (  # noqa: F401
    AdjustMode,
    DataQualityError,
    DataQualityReport,
    DataRequest,
    Frequency,
    RealtimeQuote,
    SnapshotManifest,
    STANDARD_COLUMNS as _STANDARD_COLUMNS,
)
from data.service import (  # noqa: F401
    CACHE_DIR,
    MarketDataService,
    create_data_snapshot,
    get_default_service,
    get_quality_report,
    get_suspension_dates,
    list_snapshots,
    load_daily_data,
    load_market_data,
    load_minute_data,
    load_snapshot,
    refresh_trading_calendar,
    set_realtime_provider,
    subscribe_realtime,
)

_MINUTE_PERIODS = ("1", "5", "15", "30", "60")


def run_test():
    """兼容旧入口的数据层联网冒烟测试。"""
    import datetime

    print("===== 测试开始：统一行情数据层 =====")
    end = datetime.date.today()
    start = end - datetime.timedelta(days=365 * 3)

    df = load_daily_data("600519", start, end)
    assert list(df.columns) == _STANDARD_COLUMNS
    assert len(df) > 500
    assert df.attrs.get("data_version"), "日线结果应包含数据版本"
    assert df.attrs.get("data_quality", {}).get("status") in {"PASS", "WARN"}
    print(
        f"日线通过：{len(df)} 行，版本 {df.attrs['data_version']}，"
        f"质量 {df.attrs['data_quality']['status']}"
    )

    df_min = load_minute_data("600519", period="1")
    assert list(df_min.columns) == _STANDARD_COLUMNS
    assert len(df_min) > 50
    assert df_min.attrs.get("data_version")
    print(f"分钟线通过：{len(df_min)} 行，版本 {df_min.attrs['data_version']}")

    m1 = load_market_data("600519", start, end, freq="5min")
    assert len(m1) > 10
    assert m1.attrs.get("data_version")
    print(f"统一入口通过：5 分钟线 {len(m1)} 行")
    print("===== 测试通过 =====")
    return df


if __name__ == "__main__":
    run_test()
