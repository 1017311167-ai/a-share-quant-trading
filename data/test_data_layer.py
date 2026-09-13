"""数据层离线测试，不访问网络。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pandas as pd

# 直接运行本文件时保证项目根目录在导入路径中。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.trading_calendar import TradingCalendar, weekday_calendar
from data.models import AdjustMode, DataRequest, Frequency, RealtimeQuote
from data.quality import clean_market_data, validate_market_data
from data.service import MarketDataService, normalize_realtime_quote
from data.snapshots import SnapshotStore


def _frame(start="2024-01-02", periods=3):
    index = pd.bdate_range(start, periods=periods)
    return pd.DataFrame({
        "date": index,
        "open": [10.0 + i for i in range(periods)],
        "high": [11.0 + i for i in range(periods)],
        "low": [9.0 + i for i in range(periods)],
        "close": [10.5 + i for i in range(periods)],
        "volume": [1000 + i for i in range(periods)],
    })


class FakeProvider:
    name = "fake"

    def __init__(self, frame):
        self.frame = frame.copy()
        self.calls = 0

    def fetch_history(self, request):
        self.calls += 1
        return self.frame.copy()


class FakeRealtimeProvider:
    name = "fake-realtime"

    def __init__(self):
        self.codes = None

    def subscribe_realtime(self, codes, callback=None):
        self.codes = list(codes)
        callback("600519", {
            "timestamp": "2024-01-02 09:31:00",
            "lastPrice": 100.5,
            "open": 99.8,
            "high": 101.0,
            "low": 99.5,
            "lastClose": 98.0,
            "volume": 1200,
        })


def _request(**kwargs):
    values = {
        "symbol": "600519",
        "start": "2024-01-02",
        "end": "2024-01-04",
        "frequency": "daily",
        "adjust": "qfq",
    }
    values.update(kwargs)
    return DataRequest(**values)


def _calendar():
    return TradingCalendar(
        ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"],
        source="test",
    )


def test_frequency_adjust_and_request_validation():
    assert Frequency.parse("日线") is Frequency.DAILY
    assert Frequency.parse("5min") is Frequency.MINUTE_5
    assert AdjustMode.parse("前复权") is AdjustMode.QFQ
    request = _request()
    assert request.symbol == "600519"
    assert request.frequency is Frequency.DAILY
    try:
        _request(symbol="123")
    except ValueError:
        pass
    else:
        raise AssertionError("非法股票代码应报错")
    print("PASS frequency/adjust/request")


def test_trading_calendar_and_sessions():
    calendar = _calendar()
    assert calendar.is_trading_day("2024-01-02")
    assert not calendar.is_trading_day("2024-01-06")
    assert len(calendar.expected_daily_dates("2024-01-01", "2024-01-05")) == 4
    timestamps = calendar.expected_minute_timestamps(
        "2024-01-02", "2024-01-02", Frequency.MINUTE_1
    )
    assert len(timestamps) == 240
    fallback = weekday_calendar("2024-01-01", "2024-01-07")
    assert fallback.source == "weekday-fallback"
    print("PASS trading calendar")


def test_quality_detection_and_cleaning():
    request = _request()
    frame = _frame()
    frame.loc[1, "high"] = 1.0
    frame.loc[2, "volume"] = -1
    frame = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    report = validate_market_data(
        frame, request, calendar=_calendar(), provider="test"
    )
    codes = {item.code for item in report.errors}
    assert {"DUPLICATE_TIMESTAMPS", "OHLC_INCONSISTENT", "NEGATIVE_VOLUME"} <= codes
    clean = clean_market_data(frame, Frequency.DAILY)
    assert len(clean) == 3
    assert clean["date"].is_monotonic_increasing
    print("PASS quality detection/cleaning")


def test_missing_sessions_are_suspension_candidates():
    request = _request(start="2024-01-02", end="2024-01-05")
    report = validate_market_data(
        _frame(periods=3), request, calendar=_calendar(), provider="test"
    )
    assert report.status == "WARN"
    assert "2024-01-05" in report.suspension_candidates
    assert any(item.code == "MISSING_TRADING_DAYS" for item in report.warnings)
    print("PASS missing sessions")


def test_snapshot_roundtrip_and_version():
    request = _request()
    frame = clean_market_data(_frame(), Frequency.DAILY)
    quality = validate_market_data(
        frame, request, calendar=_calendar(), provider="test"
    )
    with tempfile.TemporaryDirectory() as tmp:
        store = SnapshotStore(tmp)
        first = store.write(
            request, frame, quality, provider="test",
            calendar_version=_calendar().version,
        )
        second = store.write(
            request, frame, quality, provider="test",
            calendar_version=_calendar().version,
        )
        assert first.snapshot_id == second.snapshot_id
        loaded = store.load(first)
        assert len(loaded) == len(frame)
        found = store.find(request)
        assert found is not None and found.snapshot_id == first.snapshot_id
        assert found.content_sha256 == first.content_sha256
    print("PASS snapshot roundtrip/version")


def test_service_uses_snapshot_before_provider():
    request = _request()
    provider = FakeProvider(_frame())
    with tempfile.TemporaryDirectory() as tmp:
        store = SnapshotStore(Path(tmp) / "snapshots")
        service = MarketDataService(
            provider=provider,
            snapshot_store=store,
            cache_dir=Path(tmp) / "cache",
            calendar_loader=lambda start, end, allow_fallback: _calendar(),
        )
        first = service.load(request)
        second = service.load(request)
        assert provider.calls == 1
        assert first.attrs["data_version"] == second.attrs["data_version"]
        assert first.attrs["data_quality"]["status"] == "PASS"
        assert service.list_snapshots(symbol="600519")
    print("PASS service snapshot reuse")


def test_legacy_cache_is_versioned():
    request = _request()
    with tempfile.TemporaryDirectory() as tmp:
        cache_dir = Path(tmp) / "cache"
        cache_dir.mkdir(parents=True)
        legacy_path = cache_dir / "600519_20240102_20240104.csv"
        _frame().to_csv(legacy_path, index=False)
        provider = FakeProvider(_frame())
        service = MarketDataService(
            provider=provider,
            snapshot_store=SnapshotStore(Path(tmp) / "snapshots"),
            cache_dir=cache_dir,
            calendar_loader=lambda start, end, allow_fallback: _calendar(),
        )
        frame = service.load(request)
        assert provider.calls == 0
        assert frame.attrs["provider"] == "legacy-cache"
        assert frame.attrs["data_version"]
    print("PASS legacy cache migration")


def test_minute_service_and_adjustment_versions():
    minute_frame = pd.DataFrame({
        "date": pd.to_datetime([
            "2024-01-02 09:31:00",
            "2024-01-02 09:32:00",
            "2024-01-03 09:31:00",
        ]),
        "open": [10.0, 10.1, 10.5],
        "high": [10.2, 10.3, 10.8],
        "low": [9.9, 10.0, 10.4],
        "close": [10.1, 10.2, 10.6],
        "volume": [100, 120, 130],
    })
    provider = FakeProvider(minute_frame)
    with tempfile.TemporaryDirectory() as tmp:
        service = MarketDataService(
            provider=provider,
            snapshot_store=SnapshotStore(Path(tmp) / "snapshots"),
            cache_dir=Path(tmp) / "cache",
            calendar_loader=lambda start, end, allow_fallback: _calendar(),
        )
        minute_request = _request(
            frequency="1min",
            start="2024-01-03",
            end="2024-01-03",
        )
        loaded = service.load(minute_request)
        assert len(loaded) == 1
        assert loaded["date"].iloc[0] == pd.Timestamp("2024-01-03 09:31:00")

        qfq = service.create_snapshot(
            _request(adjust="qfq"), _frame(), provider="manual"
        )
        hfq = service.create_snapshot(
            _request(adjust="hfq"), _frame(), provider="manual"
        )
        assert qfq.snapshot_id != hfq.snapshot_id
    print("PASS minute filtering/adjustment versions")


def test_realtime_normalization_and_subscription():
    quote = normalize_realtime_quote("600519.SH", {
        "timestamp": 1704159060000,
        "lastPrice": 100.5,
        "lastClose": 98,
    }, source="test")
    assert isinstance(quote, RealtimeQuote)
    assert quote.symbol == "600519" and quote.last == 100.5
    received = []
    provider = FakeRealtimeProvider()
    service = MarketDataService(realtime_provider=provider)
    service.subscribe_realtime(["600519"], received.append)
    assert provider.codes == ["600519"]
    assert received[0].last == 100.5
    print("PASS realtime normalization/subscription")


def run_test():
    for test in (
        test_frequency_adjust_and_request_validation,
        test_trading_calendar_and_sessions,
        test_quality_detection_and_cleaning,
        test_missing_sessions_are_suspension_candidates,
        test_snapshot_roundtrip_and_version,
        test_service_uses_snapshot_before_provider,
        test_legacy_cache_is_versioned,
        test_minute_service_and_adjustment_versions,
        test_realtime_normalization_and_subscription,
    ):
        test()
    print("===== data layer tests passed =====")


if __name__ == "__main__":
    run_test()
