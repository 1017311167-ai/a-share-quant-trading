"""行情数据领域的公共类型。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


STANDARD_COLUMNS = ["date", "open", "high", "low", "close", "volume"]
SCHEMA_VERSION = "market-data-v1"


class Frequency(str, Enum):
    """统一行情频率。"""

    DAILY = "daily"
    MINUTE_1 = "1min"
    MINUTE_5 = "5min"
    MINUTE_15 = "15min"
    MINUTE_30 = "30min"
    MINUTE_60 = "60min"

    @classmethod
    def parse(cls, value) -> "Frequency":
        text = str(value).strip().lower()
        aliases = {
            "daily": cls.DAILY,
            "day": cls.DAILY,
            "d": cls.DAILY,
            "1d": cls.DAILY,
            "日线": cls.DAILY,
            "日": cls.DAILY,
            "1min": cls.MINUTE_1,
            "5min": cls.MINUTE_5,
            "15min": cls.MINUTE_15,
            "30min": cls.MINUTE_30,
            "60min": cls.MINUTE_60,
        }
        if text in aliases:
            return aliases[text]
        if text.endswith("min"):
            candidate = f"{text[:-3]}min"
            if candidate in {item.value for item in cls}:
                return cls(candidate)
        raise ValueError(
            f"不支持的行情频率：{value!r}，可选："
            "daily / 1min / 5min / 15min / 30min / 60min"
        )

    @property
    def is_daily(self) -> bool:
        return self is Frequency.DAILY

    @property
    def minutes(self) -> int | None:
        if self.is_daily:
            return None
        return int(self.value.removesuffix("min"))


class AdjustMode(str, Enum):
    """复权模式。"""

    NONE = "none"
    QFQ = "qfq"
    HFQ = "hfq"

    @classmethod
    def parse(cls, value) -> "AdjustMode":
        text = str(value).strip().lower()
        aliases = {
            "": cls.NONE,
            "none": cls.NONE,
            "raw": cls.NONE,
            "不复权": cls.NONE,
            "qfq": cls.QFQ,
            "前复权": cls.QFQ,
            "hfq": cls.HFQ,
            "后复权": cls.HFQ,
        }
        if text not in aliases:
            raise ValueError(f"不支持的复权模式：{value!r}，可选：none / qfq / hfq")
        return aliases[text]

    @property
    def provider_value(self) -> str:
        return "" if self is AdjustMode.NONE else self.value


def _as_date(value, name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        for fmt in ("%Y%m%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
    raise ValueError(
        f"{name} 日期格式不正确：{value!r}（支持 20240101、2024-01-01 或 date 对象）"
    )


@dataclass(frozen=True)
class DataRequest:
    """一次可复现的行情数据请求。"""

    symbol: str
    start: date
    end: date
    frequency: Frequency = Frequency.DAILY
    adjust: AdjustMode = AdjustMode.QFQ
    use_cache: bool = True
    strict: bool = False
    snapshot_id: str | None = None

    def __post_init__(self):
        symbol = str(self.symbol).strip()
        if not re.fullmatch(r"\d{6}", symbol):
            raise ValueError(
                f"股票代码格式不正确：{self.symbol!r}（应为 6 位数字，如 600519）"
            )
        start = _as_date(self.start, "start")
        end = _as_date(self.end, "end")
        if start > end:
            raise ValueError(f"开始日期({start})不能晚于结束日期({end})")
        frequency = self.frequency if isinstance(self.frequency, Frequency) \
            else Frequency.parse(self.frequency)
        adjust = self.adjust if isinstance(self.adjust, AdjustMode) \
            else AdjustMode.parse(self.adjust)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "frequency", frequency)
        object.__setattr__(self, "adjust", adjust)
        object.__setattr__(self, "use_cache", bool(self.use_cache))
        object.__setattr__(self, "strict", bool(self.strict))

    def to_dict(self) -> dict:
        data = asdict(self)
        data["start"] = self.start.isoformat()
        data["end"] = self.end.isoformat()
        data["frequency"] = self.frequency.value
        data["adjust"] = self.adjust.value
        return data


@dataclass
class QualityIssue:
    """一条数据质量问题。"""

    code: str
    severity: str
    message: str
    count: int = 0
    details: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DataQualityReport:
    """一次数据质量检查的完整结果。"""

    symbol: str
    frequency: str
    rows: int
    actual_start: str | None
    actual_end: str | None
    provider: str
    calendar_source: str
    calendar_version: str
    issues: list[QualityIssue] = field(default_factory=list)
    suspension_candidates: list[str] = field(default_factory=list)
    checked_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def errors(self) -> list[QualityIssue]:
        return [item for item in self.issues if item.severity == "error"]

    @property
    def warnings(self) -> list[QualityIssue]:
        return [item for item in self.issues if item.severity == "warning"]

    @property
    def status(self) -> str:
        if self.errors:
            return "FAIL"
        if self.warnings:
            return "WARN"
        return "PASS"

    @property
    def score(self) -> int:
        penalty = len(self.errors) * 20 + len(self.warnings) * 3
        return max(0, 100 - penalty)

    def add(self, code: str, severity: str, message: str,
            count: int = 0, details=None):
        self.issues.append(QualityIssue(
            code=code,
            severity=severity,
            message=message,
            count=int(count or 0),
            details=list(details or []),
        ))

    def raise_if_errors(self):
        if self.errors:
            messages = "；".join(item.message for item in self.errors)
            raise DataQualityError(f"行情数据质量检查失败：{messages}")

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "frequency": self.frequency,
            "rows": self.rows,
            "actual_start": self.actual_start,
            "actual_end": self.actual_end,
            "provider": self.provider,
            "calendar_source": self.calendar_source,
            "calendar_version": self.calendar_version,
            "status": self.status,
            "score": self.score,
            "issues": [item.to_dict() for item in self.issues],
            "suspension_candidates": list(self.suspension_candidates),
            "checked_at": self.checked_at,
        }


class DataQualityError(RuntimeError):
    """行情数据存在不可接受的质量错误。"""


@dataclass
class SnapshotManifest:
    """可复现数据快照的元数据。"""

    snapshot_id: str
    symbol: str
    frequency: str
    adjust: str
    requested_start: str
    requested_end: str
    actual_start: str | None
    actual_end: str | None
    rows: int
    columns: list[str]
    content_sha256: str
    schema_version: str
    calendar_source: str
    calendar_version: str
    provider: str
    quality: dict
    created_at: str
    code_version: str = "unknown"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SnapshotManifest":
        return cls(**data)


@dataclass(frozen=True)
class RealtimeQuote:
    """统一实时行情对象。"""

    symbol: str
    timestamp: datetime
    last: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    prev_close: float | None = None
    volume: float | None = None
    amount: float | None = None
    source: str = "unknown"
    raw: dict | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data


def stable_json_hash(data: dict, length: int = 16) -> str:
    """对 JSON 数据计算稳定哈希。"""
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]
