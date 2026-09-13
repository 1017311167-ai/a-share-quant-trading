"""A 股交易日历与交易时段。"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path

import pandas as pd

from data.models import Frequency


class TradingCalendar:
    """不可变交易日历。"""

    def __init__(self, dates, source: str = "unknown", version: str | None = None):
        normalized = sorted({_as_date(item) for item in dates})
        self._dates = frozenset(normalized)
        self.source = str(source)
        self.version = version or _hash_dates(normalized)

    @property
    def dates(self) -> tuple[dt.date, ...]:
        return tuple(sorted(self._dates))

    def is_trading_day(self, value) -> bool:
        return _as_date(value) in self._dates

    def between(self, start, end) -> list[dt.date]:
        start_date, end_date = _as_date(start), _as_date(end)
        return [item for item in self.dates if start_date <= item <= end_date]

    def expected_daily_dates(self, start, end) -> list[dt.date]:
        return self.between(start, end)

    def expected_minute_timestamps(self, start, end,
                                   frequency: Frequency | str) -> pd.DatetimeIndex:
        freq = frequency if isinstance(frequency, Frequency) else Frequency.parse(frequency)
        if freq.is_daily:
            raise ValueError("日线频率没有分钟时间戳")
        values = []
        for day in self.between(start, end):
            values.extend(_minute_bar_times(day, freq.minutes))
        return pd.DatetimeIndex(values)


def weekday_calendar(start, end) -> TradingCalendar:
    """离线兜底日历，仅根据周一至周五生成。

    该日历无法识别法定节假日，不能用于正式实盘风险判断。
    """
    start_date, end_date = _as_date(start), _as_date(end)
    dates = pd.bdate_range(start_date, end_date).date
    return TradingCalendar(dates, source="weekday-fallback")


def load_trading_calendar(start=None, end=None, *, cache_dir=None,
                          allow_fallback: bool = True,
                          refresh: bool = False,
                          fetcher=None) -> TradingCalendar:
    """加载并缓存 AKShare 交易日历。

    fetcher 返回可迭代的交易日，便于测试和替换数据源。无网络且无缓存时，
    只有 allow_fallback=True 才使用工作日兜底。
    """
    if cache_dir is None:
        cache_dir = os.environ.get("ABACKTEST_CALENDAR_DIR") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "cache", "calendar",
        )
    cache_dir = Path(cache_dir)
    data_path = cache_dir / "trade_dates.csv"
    meta_path = cache_dir / "trade_dates.meta.json"

    cached = None
    fresh = False
    if not refresh and data_path.exists() and meta_path.exists():
        try:
            frame = pd.read_csv(data_path)
            if "trade_date" in frame.columns:
                cached = [pd.to_datetime(item).date() for item in frame["trade_date"]]
        except Exception:
            cached = None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            updated_at = dt.datetime.fromisoformat(meta["updated_at"])
            fresh = (dt.datetime.now() - updated_at) < dt.timedelta(days=7)
        except Exception:
            fresh = False

    if cached is None or not fresh or refresh:
        try:
            dates = list(fetcher() if fetcher is not None else _fetch_akshare_calendar())
            calendar = TradingCalendar(dates, source="akshare")
            _write_calendar_cache(calendar, data_path, meta_path)
            cached = calendar.dates
        except Exception:
            if cached is not None:
                return TradingCalendar(cached, source="akshare")
            if not allow_fallback:
                raise
            calendar = weekday_calendar(
                start or dt.date(1990, 12, 19),
                end or dt.date.today(),
            )
            return calendar

    return TradingCalendar(cached, source="akshare")


def _fetch_akshare_calendar():
    try:
        import akshare as ak
    except ImportError as exc:
        raise RuntimeError("未安装 akshare，无法更新交易日历") from exc
    frame = ak.tool_trade_date_hist_sina()
    if "trade_date" not in frame.columns:
        raise ValueError(f"交易日历格式异常：{list(frame.columns)}")
    return [pd.to_datetime(item).date() for item in frame["trade_date"]]


def _write_calendar_cache(calendar: TradingCalendar, data_path: Path,
                          meta_path: Path):
    data_path.parent.mkdir(parents=True, exist_ok=True)
    temp_data = data_path.with_suffix(".csv.tmp")
    temp_meta = meta_path.with_suffix(".json.tmp")
    pd.DataFrame({"trade_date": [item.isoformat() for item in calendar.dates]}) \
        .to_csv(temp_data, index=False)
    temp_meta.write_text(json.dumps({
        "source": calendar.source,
        "version": calendar.version,
        "rows": len(calendar.dates),
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_data, data_path)
    os.replace(temp_meta, meta_path)


def _minute_bar_times(day: dt.date, minutes: int) -> list[pd.Timestamp]:
    if minutes not in (1, 5, 15, 30, 60):
        raise ValueError(f"不支持的分钟周期：{minutes}")
    morning = pd.date_range(
        pd.Timestamp(day) + pd.Timedelta(hours=9, minutes=30)
        + pd.Timedelta(minutes=minutes),
        pd.Timestamp(day) + pd.Timedelta(hours=11, minutes=30),
        freq=f"{minutes}min",
    )
    afternoon = pd.date_range(
        pd.Timestamp(day) + pd.Timedelta(hours=13, minutes=minutes),
        pd.Timestamp(day) + pd.Timedelta(hours=15),
        freq=f"{minutes}min",
    )
    return list(morning) + list(afternoon)


def _as_date(value) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return pd.to_datetime(value).date()


def _hash_dates(dates: list[dt.date]) -> str:
    payload = ",".join(item.isoformat() for item in dates)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
