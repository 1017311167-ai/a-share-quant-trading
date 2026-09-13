"""行情数据公共入口。

实现位于 data.service，保留本模块方便按目录职责调用。
"""

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


__all__ = [
    "CACHE_DIR",
    "MarketDataService",
    "create_data_snapshot",
    "get_default_service",
    "get_quality_report",
    "get_suspension_dates",
    "list_snapshots",
    "load_daily_data",
    "load_market_data",
    "load_minute_data",
    "load_snapshot",
    "refresh_trading_calendar",
    "set_realtime_provider",
    "subscribe_realtime",
]
