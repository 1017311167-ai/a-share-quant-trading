"""行情数据清洗和质量检查。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from data.trading_calendar import TradingCalendar
from data.models import (
    STANDARD_COLUMNS,
    DataQualityReport,
    DataRequest,
)


def clean_market_data(df: pd.DataFrame, frequency) -> pd.DataFrame:
    """执行不改变价格含义的确定性清洗。

    只处理日期类型、排序、重复时间和缺失成交量。价格异常不会被静默修正，
    而是交给质量检查报告。
    """
    if df is None or len(df) == 0:
        raise ValueError("行情数据为空")
    missing = [column for column in STANDARD_COLUMNS if column not in df.columns]
    if missing:
        raise ValueError(f"行情数据缺少列：{missing}")

    out = df[STANDARD_COLUMNS].copy()
    input_rows = len(out)
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        out[column] = pd.to_numeric(out[column], errors="coerce")
    out = out.dropna(subset=["date", "open", "high", "low", "close"])
    if out.empty:
        raise ValueError("行情数据清洗后为空，OHLC 价格可能全部无效")
    out["volume"] = out["volume"].fillna(0)
    duplicate_rows = int(out.duplicated(subset=["date"], keep=False).sum())
    out = out.sort_values("date", kind="stable")
    out = out.drop_duplicates(subset=["date"], keep="last")
    out = out.reset_index(drop=True)
    if not out.empty and np.isfinite(out["volume"]).all():
        out["volume"] = out["volume"].astype("int64")
    out.attrs["frequency"] = str(getattr(frequency, "value", frequency))
    out.attrs["input_rows"] = input_rows
    out.attrs["rows_dropped"] = input_rows - len(out)
    out.attrs["duplicate_rows"] = duplicate_rows
    return out


def validate_market_data(df: pd.DataFrame, request: DataRequest, *,
                         calendar: TradingCalendar | None = None,
                         provider: str = "unknown") -> DataQualityReport:
    """检查行情数据并生成可持久化的质量报告。"""
    calendar = calendar or TradingCalendar([], source="unavailable", version="unavailable")
    report = DataQualityReport(
        symbol=request.symbol,
        frequency=request.frequency.value,
        rows=0 if df is None else len(df),
        actual_start=None,
        actual_end=None,
        provider=provider,
        calendar_source=calendar.source,
        calendar_version=calendar.version,
    )
    if df is None or len(df) == 0:
        report.add("EMPTY_DATA", "error", "行情数据为空")
        return report

    missing = [column for column in STANDARD_COLUMNS if column not in df.columns]
    if missing:
        report.add("MISSING_COLUMNS", "error", f"缺少标准列：{missing}")
        return report

    frame = df.copy()
    parsed_dates = pd.to_datetime(frame["date"], errors="coerce")
    if parsed_dates.isna().any():
        report.add("INVALID_DATE", "error",
                   f"存在无法解析的日期：{int(parsed_dates.isna().sum())} 行")
        return report
    frame["date"] = parsed_dates
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    report.rows = len(frame)
    report.actual_start = frame["date"].min().isoformat()
    report.actual_end = frame["date"].max().isoformat()

    dropped = int(frame.attrs.get("rows_dropped", 0))
    if dropped:
        report.add(
            "ROWS_DROPPED",
            "warning",
            f"清洗时丢弃 {dropped} 行缺少日期或 OHLC 的数据",
            dropped,
        )

    duplicates = int(frame.duplicated(subset=["date"], keep=False).sum())
    duplicates = max(duplicates, int(frame.attrs.get("duplicate_rows", 0)))
    if duplicates:
        report.add("DUPLICATE_TIMESTAMPS", "error",
                   f"存在重复时间戳：{duplicates} 行", duplicates)
    if not frame["date"].is_monotonic_increasing:
        report.add("NON_MONOTONIC", "warning", "数据未按时间升序排列")

    price_columns = ["open", "high", "low", "close"]
    non_positive = frame[
        (frame[price_columns] <= 0).any(axis=1)
    ]
    if len(non_positive):
        report.add("NON_POSITIVE_PRICE", "error",
                   f"存在非正价格：{len(non_positive)} 行",
                   len(non_positive), _date_details(non_positive))

    inconsistent = frame[
        (frame["high"] < frame["low"])
        | (frame["high"] < frame[["open", "close"]].max(axis=1))
        | (frame["low"] > frame[["open", "close"]].min(axis=1))
    ]
    if len(inconsistent):
        report.add("OHLC_INCONSISTENT", "error",
                   f"OHLC 关系不一致：{len(inconsistent)} 行",
                   len(inconsistent), _date_details(inconsistent))

    negative_volume = frame[frame["volume"] < 0]
    if len(negative_volume):
        report.add("NEGATIVE_VOLUME", "error",
                   f"成交量为负数：{len(negative_volume)} 行",
                   len(negative_volume), _date_details(negative_volume))

    zero_ratio = float((frame["volume"] == 0).mean())
    if zero_ratio > 0.5:
        report.add("HIGH_ZERO_VOLUME_RATIO", "warning",
                   f"零成交量占比过高：{zero_ratio:.2%}")

    _check_missing_sessions(frame, request, calendar, report)
    _check_price_outliers(frame, request, report)
    return report


def suspension_candidates(df: pd.DataFrame, request: DataRequest,
                          calendar: TradingCalendar) -> list[str]:
    """根据交易日历和实际行情识别疑似停牌或缺失日期。"""
    if request.frequency.is_daily:
        expected = set(calendar.expected_daily_dates(request.start, request.end))
        actual = set(pd.to_datetime(df["date"]).dt.date) if len(df) else set()
        return [item.isoformat() for item in sorted(expected - actual)]

    expected = set(calendar.expected_minute_timestamps(
        request.start, request.end, request.frequency))
    actual = set(pd.to_datetime(df["date"])) if len(df) else set()
    return [item.isoformat() for item in sorted(expected - actual)]


def _check_missing_sessions(frame: pd.DataFrame, request: DataRequest,
                            calendar: TradingCalendar,
                            report: DataQualityReport):
    if not calendar.dates:
        return
    missing = suspension_candidates(frame, request, calendar)
    if not missing:
        return
    report.suspension_candidates = missing[:200]
    if request.frequency.is_daily:
        report.add(
            "MISSING_TRADING_DAYS",
            "warning",
            f"相对交易日历缺少 {len(missing)} 个交易日，可能是停牌或数据缺失",
            len(missing),
            missing[:50],
        )
    else:
        report.add(
            "MISSING_MINUTE_BARS",
            "warning",
            f"相对交易时段缺少 {len(missing)} 根分钟线",
            len(missing),
            missing[:50],
        )


def _check_price_outliers(frame: pd.DataFrame, request: DataRequest,
                          report: DataQualityReport):
    close = pd.to_numeric(frame["close"], errors="coerce")
    returns = np.log(close.where(close > 0)).diff().abs()
    threshold = 0.35 if request.frequency.is_daily else 0.15
    outliers = frame[returns > threshold]
    if len(outliers):
        report.add(
            "PRICE_OUTLIERS",
            "warning",
            f"存在 {len(outliers)} 个超过阈值的价格跳变，需确认复权和公司行为",
            len(outliers),
            _date_details(outliers),
        )


def _date_details(frame: pd.DataFrame, limit: int = 20) -> list[str]:
    return [
        pd.Timestamp(item).isoformat()
        for item in frame["date"].head(limit).tolist()
    ]
