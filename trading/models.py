"""模拟盘运行、信号和回放事件模型。"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from enum import Enum


class SignalAction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    NOOP = "noop"


@dataclass(frozen=True)
class TradingSignal:
    """策略运行时发出的标准交易信号。"""

    symbol: str
    action: SignalAction
    quantity: int
    limit_price: float
    strategy_instance_id: str
    idempotency_key: str
    signal_time: dt.datetime = field(default_factory=dt.datetime.now)
    data_version: str = ""
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    metadata: dict = field(default_factory=dict)
    signal_status: str = "generated"

    def __post_init__(self):
        if isinstance(self.action, str):
            object.__setattr__(
                self, "action", SignalAction(self.action.lower())
            )
        if not self.symbol:
            raise ValueError("信号 symbol 不能为空")
        if not self.strategy_instance_id:
            raise ValueError("信号 strategy_instance_id 不能为空")
        if not self.idempotency_key:
            raise ValueError("信号 idempotency_key 不能为空")
        if int(self.quantity) < 0:
            raise ValueError("信号 quantity 不能为负数")
        if float(self.limit_price) < 0:
            raise ValueError("信号 limit_price 不能为负数")
        if (
            self.action is not SignalAction.NOOP
            and (int(self.quantity) <= 0 or float(self.limit_price) <= 0)
        ):
            raise ValueError("买卖信号必须提供正数数量和限价")
        object.__setattr__(self, "quantity", int(self.quantity))
        object.__setattr__(self, "limit_price", float(self.limit_price))

    def to_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "strategy_instance_id": self.strategy_instance_id,
            "symbol": self.symbol,
            "action": self.action.value,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "signal_time": self.signal_time.isoformat(),
            "data_version": self.data_version,
            "idempotency_key": self.idempotency_key,
            "metadata": dict(self.metadata),
            "signal_status": self.signal_status,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TradingSignal":
        values = dict(data)
        values["action"] = SignalAction(str(values["action"]).lower())
        if values.get("signal_time"):
            values["signal_time"] = dt.datetime.fromisoformat(
                str(values["signal_time"])
            )
        return cls(**values)


@dataclass(frozen=True)
class ReplayEvent:
    """行情、信号和故障演练事件。"""

    event_type: str
    at: dt.datetime
    payload: dict = field(default_factory=dict)

    def __post_init__(self):
        allowed = {
            "quote",
            "signal",
            "cancel",
            "poll",
            "reconcile",
            "disconnect",
            "reconnect",
            "settle",
        }
        if self.event_type not in allowed:
            raise ValueError(f"不支持的回放事件：{self.event_type!r}")
