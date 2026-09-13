"""统一策略信号输出。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from strategies.metadata import StrategyMetadata, parameter_hash


SIGNAL_SCHEMA_VERSION = "signals-v1"


@dataclass
class SignalOutput:
    """一组经过校验的策略信号及可追踪身份。"""

    entries: pd.Series
    exits: pd.Series
    strategy_key: str
    strategy_version: str
    metadata_hash: str
    parameter_hash: str
    data_version: str
    signal_hash: str
    created_at: str

    def to_frame(self) -> pd.DataFrame:
        """输出 date、signal、entry、exit 标准信号表。"""
        signal = pd.Series(0, index=self.entries.index, dtype="int8")
        signal.loc[self.entries] = 1
        signal.loc[self.exits] = -1
        dates = (
            self.entries.index
            if isinstance(self.entries.index, pd.DatetimeIndex)
            else self.entries.index.to_series().reset_index(drop=True)
        )
        return pd.DataFrame({
            "date": list(dates),
            "signal": signal.to_numpy(),
            "entry": self.entries.astype(bool).to_numpy(),
            "exit": self.exits.astype(bool).to_numpy(),
        })

    def to_dict(self) -> dict:
        return {
            "schema_version": SIGNAL_SCHEMA_VERSION,
            "strategy_key": self.strategy_key,
            "strategy_version": self.strategy_version,
            "metadata_hash": self.metadata_hash,
            "parameter_hash": self.parameter_hash,
            "data_version": self.data_version,
            "signal_hash": self.signal_hash,
            "entry_count": int(self.entries.sum()),
            "exit_count": int(self.exits.sum()),
            "rows": len(self.entries),
            "created_at": self.created_at,
        }


def build_signal_output(strategy, df: pd.DataFrame, entries, exits) -> SignalOutput:
    """校验 Strategy 子类的原始信号并生成标准信号对象。"""
    if df is None or len(df) == 0:
        raise ValueError("生成策略信号时行情数据为空")
    metadata: StrategyMetadata = strategy.metadata()
    missing = [column for column in metadata.required_columns if column not in df.columns]
    if missing:
        raise ValueError(f"策略 {metadata.key} 缺少行情列：{missing}")
    entry_series = _normalize_series(entries, df, "entries")
    exit_series = _normalize_series(exits, df, "exits")
    conflict = int((entry_series & exit_series).sum())
    if conflict:
        raise ValueError(f"策略 {metadata.key} 同一天出现 {conflict} 个买入和卖出信号")
    params = strategy.params()
    data_version = str(df.attrs.get("data_version", "unknown"))
    param_hash = parameter_hash(params)
    signal_hash = _signal_hash(
        metadata=metadata,
        params=params,
        data_version=data_version,
        entries=entry_series,
        exits=exit_series,
    )
    return SignalOutput(
        entries=entry_series,
        exits=exit_series,
        strategy_key=metadata.key,
        strategy_version=metadata.version,
        metadata_hash=metadata.metadata_hash,
        parameter_hash=param_hash,
        data_version=data_version,
        signal_hash=signal_hash,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )


def _normalize_series(values, df: pd.DataFrame, name: str) -> pd.Series:
    series = pd.Series(values)
    if len(series) != len(df):
        raise ValueError(f"{name} 长度({len(series)})与行情数据({len(df)})不一致")
    series = series.fillna(False).astype(bool)
    if not series.index.equals(df.index):
        series = pd.Series(series.to_numpy(), index=df.index)
    return series


def _signal_hash(metadata: StrategyMetadata, params: dict, data_version: str,
                 entries: pd.Series, exits: pd.Series) -> str:
    frame = pd.DataFrame({
        "date": [str(item) for item in entries.index],
        "entry": entries.astype("int8").to_numpy(),
        "exit": exits.astype("int8").to_numpy(),
    })
    identity = {
        "strategy": metadata.key,
        "strategy_version": metadata.version,
        "metadata_hash": metadata.metadata_hash,
        "parameters": params,
        "data_version": data_version,
    }
    digest = hashlib.sha256()
    digest.update(repr(sorted(identity.items())).encode("utf-8"))
    digest.update(frame.to_csv(index=False, lineterminator="\n").encode("utf-8"))
    return digest.hexdigest()[:20]
