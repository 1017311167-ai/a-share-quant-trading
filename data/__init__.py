"""A 股行情数据层。

统一提供历史日线、分钟线、实时行情、交易日历、质量检查和版本化快照。
"""

from data.trading_calendar import TradingCalendar, load_trading_calendar  # noqa: F401
from data.loader import (  # noqa: F401
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
from data.models import (  # noqa: F401
    AdjustMode,
    DataQualityError,
    DataQualityReport,
    DataRequest,
    Frequency,
    RealtimeQuote,
    SnapshotManifest,
)


__all__ = [
    "AdjustMode",
    "CACHE_DIR",
    "DataQualityError",
    "DataQualityReport",
    "DataRequest",
    "Frequency",
    "MarketDataService",
    "RealtimeQuote",
    "SnapshotManifest",
    "TradingCalendar",
    "create_data_snapshot",
    "get_default_service",
    "get_quality_report",
    "get_suspension_dates",
    "list_snapshots",
    "load_daily_data",
    "load_market_data",
    "load_minute_data",
    "load_snapshot",
    "load_trading_calendar",
    "refresh_trading_calendar",
    "set_realtime_provider",
    "subscribe_realtime",
]
