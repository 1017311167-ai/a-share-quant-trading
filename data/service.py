"""统一行情数据服务和兼容入口。"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pandas as pd

from data.trading_calendar import TradingCalendar, load_trading_calendar
from data.models import (
    AdjustMode,
    DataQualityReport,
    DataRequest,
    Frequency,
    RealtimeQuote,
    SnapshotManifest,
)
from data.providers import AkshareHistoryProvider
from data.quality import clean_market_data, suspension_candidates, validate_market_data
from data.snapshots import SnapshotStore
from utils.versioning import get_code_version


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_DIR = os.environ.get("ABACKTEST_CACHE_DIR") or str(
    PROJECT_ROOT / "data" / "cache"
)
DEFAULT_SNAPSHOT_DIR = os.environ.get("ABACKTEST_SNAPSHOT_DIR") or str(
    PROJECT_ROOT / "data" / "snapshots"
)
CACHE_DIR = DEFAULT_CACHE_DIR


class MarketDataService:
    """统一日线、分钟线和实时行情的数据服务。"""

    def __init__(self, *, provider=None, realtime_provider=None,
                 snapshot_store=None, cache_dir=None, calendar_loader=None):
        self.provider = provider or AkshareHistoryProvider()
        self.realtime_provider = realtime_provider
        self.snapshot_store = snapshot_store or SnapshotStore(
            DEFAULT_SNAPSHOT_DIR
        )
        self.cache_dir = Path(cache_dir or DEFAULT_CACHE_DIR)
        self.calendar_loader = calendar_loader or load_trading_calendar
        self._calendar_cache = {}

    def load(self, request: DataRequest) -> pd.DataFrame:
        """加载行情，优先使用请求快照、版本化快照和兼容缓存。"""
        if request.snapshot_id:
            manifest = self.snapshot_store.get(
                request.snapshot_id, symbol=request.symbol
            )
            return _attach_metadata(self.snapshot_store.load(manifest), manifest)

        if request.use_cache:
            manifest = self.snapshot_store.find(request)
            if manifest is not None:
                current_calendar = self._get_calendar(request)
                if (
                    manifest.calendar_source == "akshare"
                    or manifest.calendar_source == current_calendar.source
                ):
                    frame = self.snapshot_store.load(manifest)
                    return _attach_metadata(frame, manifest)

            legacy = self._read_legacy_cache(request)
            if legacy is not None:
                frame = clean_market_data(legacy, request.frequency)
                calendar = self._get_calendar(request)
                report = validate_market_data(
                    frame, request, calendar=calendar, provider="legacy-cache"
                )
                report.raise_if_errors()
                manifest = self.snapshot_store.write(
                    request,
                    frame,
                    report,
                    provider="legacy-cache",
                    calendar_version=calendar.version,
                    code_version=_code_version(),
                )
                return _attach_metadata(frame, manifest)

        raw = self.provider.fetch_history(request)
        frame = clean_market_data(raw, request.frequency)
        frame = _filter_request_range(frame, request)
        if frame.empty:
            raise ValueError(
                f"{request.symbol} 在 {request.start}~{request.end} 没有行情数据"
            )
        calendar = self._get_calendar(request)
        report = validate_market_data(
            frame, request, calendar=calendar, provider=self.provider.name
        )
        report.raise_if_errors()
        manifest = self.snapshot_store.write(
            request,
            frame,
            report,
            provider=self.provider.name,
            calendar_version=calendar.version,
            code_version=_code_version(),
        )
        if request.use_cache:
            self._write_legacy_cache(frame, request)
        return _attach_metadata(frame, manifest)

    def check_quality(self, request: DataRequest, frame: pd.DataFrame | None = None,
                      *, provider: str | None = None) -> DataQualityReport:
        """对指定数据执行质量检查。"""
        if frame is None:
            frame = self.load(request)
        calendar = self._get_calendar(request)
        return validate_market_data(
            frame,
            request,
            calendar=calendar,
            provider=provider or frame.attrs.get("provider", "unknown"),
        )

    def create_snapshot(self, request: DataRequest, frame: pd.DataFrame, *,
                        provider: str = "manual",
                        calendar: TradingCalendar | None = None) -> SnapshotManifest:
        """将已有数据显式保存为可复现版本。"""
        clean = clean_market_data(frame, request.frequency)
        clean = _filter_request_range(clean, request)
        calendar = calendar or self._get_calendar(request)
        report = validate_market_data(
            clean, request, calendar=calendar, provider=provider
        )
        report.raise_if_errors()
        return self.snapshot_store.write(
            request,
            clean,
            report,
            provider=provider,
            calendar_version=calendar.version,
            code_version=_code_version(),
        )

    def load_snapshot(self, snapshot_id: str, *, symbol: str | None = None) -> pd.DataFrame:
        manifest = self.snapshot_store.get(snapshot_id, symbol=symbol)
        frame = self.snapshot_store.load(manifest)
        return _attach_metadata(frame, manifest)

    def list_snapshots(self, *, symbol: str | None = None) -> list[SnapshotManifest]:
        return self.snapshot_store.list(symbol=symbol)

    def suspension_dates(self, request: DataRequest,
                         frame: pd.DataFrame | None = None) -> list[str]:
        frame = self.load(request) if frame is None else frame
        return suspension_candidates(frame, request, self._get_calendar(request))

    def subscribe_realtime(self, codes, callback, *, provider=None):
        """订阅统一实时行情并回调 RealtimeQuote。"""
        provider = provider or self.realtime_provider
        if provider is None:
            raise ValueError(
                "未配置实时行情提供器。可将 QMT broker 或实现 "
                "subscribe_realtime(codes, callback) 的对象注入 realtime_provider。"
            )
        normalized_codes = _normalize_codes(codes)
        source = getattr(provider, "name", provider.__class__.__name__)

        def on_raw(code, data):
            quote = normalize_realtime_quote(code, data, source=source)
            callback(quote)

        return provider.subscribe_realtime(normalized_codes, callback=on_raw)

    def set_realtime_provider(self, provider):
        self.realtime_provider = provider

    def _get_calendar(self, request: DataRequest) -> TradingCalendar:
        key = (request.start, request.end, request.strict)
        if key not in self._calendar_cache:
            self._calendar_cache[key] = self.calendar_loader(
                request.start,
                request.end,
                allow_fallback=not request.strict,
            )
        return self._calendar_cache[key]

    def _read_legacy_cache(self, request: DataRequest) -> pd.DataFrame | None:
        if request.adjust is not AdjustMode.QFQ:
            return None
        path = self._legacy_cache_path(request)
        if path is None or not path.exists():
            return None
        try:
            frame = pd.read_csv(path)
        except Exception:
            return None
        if list(frame.columns) != [
            "date", "open", "high", "low", "close", "volume"
        ]:
            return None
        return frame

    def _write_legacy_cache(self, frame: pd.DataFrame, request: DataRequest):
        path = self._legacy_cache_path(request)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        frame.to_csv(temp, index=False)
        os.replace(temp, path)

    def _legacy_cache_path(self, request: DataRequest) -> Path | None:
        if request.adjust is not AdjustMode.QFQ:
            return None
        if request.frequency.is_daily:
            name = (
                f"{request.symbol}_{request.start.strftime('%Y%m%d')}_"
                f"{request.end.strftime('%Y%m%d')}.csv"
            )
        else:
            name = (
                f"minute_{request.symbol}_{request.frequency.value}_"
                f"{dt.date.today().strftime('%Y%m%d')}.csv"
            )
        return self.cache_dir / name


def normalize_realtime_quote(symbol: str, data, *, source: str = "unknown") -> RealtimeQuote:
    """把不同数据源的实时行情字典转换为统一对象。"""
    if not isinstance(data, dict):
        raise ValueError(f"实时行情格式不正确：{data!r}")
    last = _first_number(data, "lastPrice", "last", "price", "close")
    if last is None or last <= 0:
        raise ValueError(f"实时行情缺少有效最新价：{data!r}")
    timestamp = _parse_timestamp(
        data.get("timestamp")
        or data.get("time")
        or data.get("timetag")
        or data.get("datetime")
    )
    return RealtimeQuote(
        symbol=str(symbol).split(".")[0],
        timestamp=timestamp,
        last=last,
        open=_first_number(data, "open", "openPrice"),
        high=_first_number(data, "high", "highPrice"),
        low=_first_number(data, "low", "lowPrice"),
        prev_close=_first_number(data, "prev_close", "prevClose", "lastClose"),
        volume=_first_number(data, "volume", "vol"),
        amount=_first_number(data, "amount", "turnover"),
        source=source,
        raw=dict(data),
    )


def _first_number(data: dict, *keys) -> float | None:
    for key in keys:
        value = data.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _parse_timestamp(value) -> dt.datetime:
    if value is None or value == "":
        return dt.datetime.now()
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time())
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return dt.datetime.fromtimestamp(number)
    except (TypeError, ValueError):
        pass
    try:
        return pd.Timestamp(value).to_pydatetime()
    except Exception:
        return dt.datetime.now()


def _filter_request_range(frame: pd.DataFrame, request: DataRequest) -> pd.DataFrame:
    out = frame
    if request.frequency.is_daily:
        start = pd.Timestamp(request.start)
        end = pd.Timestamp(request.end) + pd.Timedelta(days=1)
    else:
        start = pd.Timestamp(request.start)
        end = pd.Timestamp(request.end) + pd.Timedelta(days=1)
    return out[(out["date"] >= start) & (out["date"] < end)].reset_index(drop=True)


def _normalize_codes(codes) -> list[str]:
    values = [codes] if isinstance(codes, str) else list(codes)
    if not values:
        raise ValueError("实时行情订阅至少需要 1 个股票代码")
    return [str(item).split(".")[0].strip() for item in values]


def _attach_metadata(frame: pd.DataFrame,
                     manifest: SnapshotManifest) -> pd.DataFrame:
    out = frame.copy()
    out.attrs["data_version"] = manifest.snapshot_id
    out.attrs["schema_version"] = manifest.schema_version
    out.attrs["provider"] = manifest.provider
    out.attrs["adjust"] = manifest.adjust
    out.attrs["snapshot_manifest"] = manifest.to_dict()
    out.attrs["data_quality"] = manifest.quality
    return out


def _code_version() -> str:
    return get_code_version(PROJECT_ROOT)


_DEFAULT_SERVICE: MarketDataService | None = None


def get_default_service() -> MarketDataService:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        _DEFAULT_SERVICE = MarketDataService()
    return _DEFAULT_SERVICE


def set_realtime_provider(provider):
    get_default_service().set_realtime_provider(provider)


def load_daily_data(code: str, start, end, use_cache: bool = True, *,
                    adjust="qfq", strict: bool = False,
                    snapshot_id: str | None = None) -> pd.DataFrame:
    request = DataRequest(
        symbol=code,
        start=start,
        end=end,
        frequency=Frequency.DAILY,
        adjust=adjust,
        use_cache=use_cache,
        strict=strict,
        snapshot_id=snapshot_id,
    )
    return get_default_service().load(request)


def load_minute_data(code: str, period="1", start=None, end=None,
                     use_cache: bool = True, *, adjust="qfq",
                     strict: bool = False,
                     snapshot_id: str | None = None) -> pd.DataFrame:
    freq = Frequency.parse(f"{period}min")
    if start is None:
        start = dt.date.today() - dt.timedelta(days=6)
    if end is None:
        end = dt.date.today()
    request = DataRequest(
        symbol=code,
        start=start,
        end=end,
        frequency=freq,
        adjust=adjust,
        use_cache=use_cache,
        strict=strict,
        snapshot_id=snapshot_id,
    )
    return get_default_service().load(request)


def load_market_data(code: str, start, end, freq: str = "daily",
                     use_cache: bool = True, *, adjust="qfq",
                     strict: bool = False,
                     snapshot_id: str | None = None) -> pd.DataFrame:
    frequency = Frequency.parse(freq)
    request = DataRequest(
        symbol=code,
        start=start,
        end=end,
        frequency=frequency,
        adjust=adjust,
        use_cache=use_cache,
        strict=strict,
        snapshot_id=snapshot_id,
    )
    return get_default_service().load(request)


def create_data_snapshot(code: str, start, end, df: pd.DataFrame, *,
                         freq: str = "daily", adjust="qfq",
                         provider: str = "manual"):
    request = DataRequest(
        symbol=code,
        start=start,
        end=end,
        frequency=Frequency.parse(freq),
        adjust=adjust,
        use_cache=False,
    )
    return get_default_service().create_snapshot(
        request, df, provider=provider
    )


def load_snapshot(snapshot_id: str, *, symbol: str | None = None) -> pd.DataFrame:
    return get_default_service().load_snapshot(snapshot_id, symbol=symbol)


def list_snapshots(*, symbol: str | None = None) -> list[SnapshotManifest]:
    return get_default_service().list_snapshots(symbol=symbol)


def get_quality_report(code: str, start, end, *, freq: str = "daily",
                       adjust="qfq", strict: bool = False) -> DataQualityReport:
    request = DataRequest(
        symbol=code,
        start=start,
        end=end,
        frequency=Frequency.parse(freq),
        adjust=adjust,
        strict=strict,
    )
    frame = get_default_service().load(request)
    return get_default_service().check_quality(request, frame)


def get_suspension_dates(code: str, start, end, *, freq: str = "daily",
                         adjust="qfq", strict: bool = False) -> list[str]:
    request = DataRequest(
        symbol=code,
        start=start,
        end=end,
        frequency=Frequency.parse(freq),
        adjust=adjust,
        strict=strict,
    )
    return get_default_service().suspension_dates(request)


def subscribe_realtime(codes, callback, *, provider=None):
    return get_default_service().subscribe_realtime(
        codes, callback, provider=provider
    )


def refresh_trading_calendar(start=None, end=None, *, refresh: bool = True,
                             allow_fallback: bool = False):
    return load_trading_calendar(
        start,
        end,
        allow_fallback=allow_fallback,
        refresh=refresh,
    )
