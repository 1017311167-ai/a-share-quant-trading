"""行情历史数据提供器。"""

from __future__ import annotations

import logging
import time
from typing import Protocol

import pandas as pd

from data.models import DataRequest, STANDARD_COLUMNS

logger = logging.getLogger(__name__)

RETRY_INTERVAL = 2


class HistoryProvider(Protocol):
    """历史行情提供器协议。"""

    name: str

    def fetch_history(self, request: DataRequest) -> pd.DataFrame:
        """返回标准 OHLCV DataFrame。"""


class RealtimeProvider(Protocol):
    """实时行情提供器协议。"""

    name: str

    def subscribe_realtime(self, codes, callback=None):
        """订阅实时行情并以原始行情字典回调。"""


class AkshareHistoryProvider:
    """AKShare 日线和分钟线提供器。"""

    name = "akshare"

    def __init__(self, min_interval: float = RETRY_INTERVAL):
        self.min_interval = float(min_interval)

    def fetch_history(self, request: DataRequest) -> pd.DataFrame:
        if request.frequency.is_daily:
            return self._fetch_daily(request)
        return self._fetch_minute(request)

    def _fetch_daily(self, request: DataRequest) -> pd.DataFrame:
        sources = (
            ("腾讯", self._fetch_tencent, 2),
            ("东方财富", self._fetch_eastmoney, 1),
        )
        errors = []
        for name, fetcher, retries in sources:
            for attempt in range(1, retries + 1):
                try:
                    logger.info("从%s下载 %s 日线数据", name, request.symbol)
                    frame = fetcher(request)
                    if frame is None or frame.empty:
                        raise ValueError("数据源返回空数据")
                    return frame
                except Exception as exc:
                    errors.append(f"{name}：{exc}")
                    if attempt < retries:
                        time.sleep(self.min_interval)
        raise RuntimeError("所有日线数据源均下载失败：\n" + "\n".join(errors))

    def _fetch_minute(self, request: DataRequest) -> pd.DataFrame:
        errors = []
        for attempt in range(1, 3):
            try:
                logger.info("从新浪下载 %s %s 数据",
                            request.symbol, request.frequency.value)
                frame = self._fetch_sina_minute(request)
                if frame is None or frame.empty:
                    raise ValueError("数据源返回空数据")
                return frame
            except Exception as exc:
                errors.append(str(exc))
                if attempt < 2:
                    time.sleep(self.min_interval)
        raise RuntimeError(
            "分钟线数据下载失败：\n" + "\n".join(errors) +
            "\n提示：新浪分钟线接口偶尔不稳定，请稍后重试。"
        )

    def _fetch_tencent(self, request: DataRequest) -> pd.DataFrame:
        ak = _require_akshare()
        frame = ak.stock_zh_a_hist_tx(
            symbol=_with_market_prefix(request.symbol),
            start_date=request.start.strftime("%Y%m%d"),
            end_date=request.end.strftime("%Y%m%d"),
            adjust=request.adjust.provider_value,
        )
        return _standard_columns(frame)

    def _fetch_eastmoney(self, request: DataRequest) -> pd.DataFrame:
        ak = _require_akshare()
        frame = ak.stock_zh_a_hist(
            symbol=request.symbol,
            period="daily",
            start_date=request.start.strftime("%Y%m%d"),
            end_date=request.end.strftime("%Y%m%d"),
            adjust=request.adjust.provider_value,
        )
        column_map = {
            "日期": "date",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "收盘": "close",
            "成交量": "volume",
        }
        frame = frame.rename(columns=column_map)
        frame = _standard_columns(frame)
        frame["volume"] = frame["volume"] * 100
        return frame

    def _fetch_sina_minute(self, request: DataRequest) -> pd.DataFrame:
        ak = _require_akshare()
        frame = ak.stock_zh_a_minute(
            symbol=_with_market_prefix(request.symbol),
            period=str(request.frequency.minutes),
            adjust=request.adjust.provider_value,
        )
        if "day" in frame.columns:
            frame = frame.rename(columns={"day": "date"})
        return _standard_columns(frame)


def _standard_columns(frame: pd.DataFrame) -> pd.DataFrame:
    missing = [column for column in STANDARD_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"数据源返回列不符合标准，缺少：{missing}")
    return frame[STANDARD_COLUMNS].copy()


def _with_market_prefix(code: str) -> str:
    if code.startswith("6"):
        return f"sh{code}"
    if code.startswith(("0", "3")):
        return f"sz{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    raise ValueError(f"暂不支持该股票代码：{code}")


def _require_akshare():
    try:
        import akshare as ak
    except ImportError as exc:
        raise ImportError("未安装 akshare，请先运行：pip install -r requirements.txt") from exc
    return ak
