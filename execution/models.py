"""订单执行管理器的数据模型。"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from broker_adapter.models import OrderSide, OrderStatus


class ExecutionStatus(str, Enum):
    CREATED = "created"
    PENDING_SUBMIT = "pending_submit"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    CANCEL_PENDING = "cancel_pending"
    UNKNOWN = "unknown"
    RECONCILING = "reconciling"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {
            ExecutionStatus.FILLED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.REJECTED,
            ExecutionStatus.EXPIRED,
            ExecutionStatus.FAILED,
        }


@dataclass(frozen=True)
class ExecutionPolicy:
    """订单执行策略。"""

    max_attempts: int = 3
    submit_retry_delay_seconds: float = 1.0
    cancel_after_seconds: float = 30.0
    chase_after_seconds: float = 5.0
    max_chase_steps: int = 3
    chase_step_ticks: int = 1
    price_tick: float = 0.01
    unknown_grace_seconds: float = 5.0
    retry_on_unknown: bool = True
    retry_on_rejected: bool = False
    max_price_deviation_pct: float = 0.01

    def __post_init__(self):
        for name in (
            "submit_retry_delay_seconds",
            "cancel_after_seconds",
            "chase_after_seconds",
            "unknown_grace_seconds",
        ):
            if float(getattr(self, name)) < 0:
                raise ValueError(f"{name} 不能为负数")
        if self.max_attempts < 1:
            raise ValueError("max_attempts 必须为正整数")
        if self.max_chase_steps < 0 or self.chase_step_ticks < 0:
            raise ValueError("追价步数和每步 tick 不能为负数")
        if self.price_tick <= 0:
            raise ValueError("price_tick 必须大于 0")
        if self.max_price_deviation_pct < 0:
            raise ValueError("max_price_deviation_pct 不能为负数")

    @property
    def policy_hash(self) -> str:
        import hashlib
        import json

        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]


@dataclass(frozen=True)
class OrderIntent:
    """执行管理器接收的稳定业务意图。"""

    idempotency_key: str
    symbol: str
    side: OrderSide
    quantity: int
    limit_price: float
    hard_limit_price: float | None = None
    strategy_id: str = ""
    expires_at: dt.datetime | None = None
    metadata: dict = field(default_factory=dict)
    intent_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self):
        if isinstance(self.side, str):
            object.__setattr__(self, "side", OrderSide(self.side.lower()))
        if not self.idempotency_key:
            raise ValueError("idempotency_key 不能为空")
        if not self.symbol:
            raise ValueError("symbol 不能为空")
        if int(self.quantity) <= 0:
            raise ValueError("quantity 必须大于 0")
        if float(self.limit_price) <= 0:
            raise ValueError("limit_price 必须大于 0")
        object.__setattr__(self, "quantity", int(self.quantity))
        object.__setattr__(self, "limit_price", float(self.limit_price))
        if self.hard_limit_price is not None:
            object.__setattr__(
                self, "hard_limit_price", float(self.hard_limit_price)
            )

    def to_dict(self) -> dict:
        data = asdict(self)
        data["side"] = self.side.value
        data["expires_at"] = (
            self.expires_at.isoformat() if self.expires_at else None
        )
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "OrderIntent":
        values = dict(data)
        values["side"] = OrderSide(values["side"])
        if values.get("expires_at"):
            values["expires_at"] = dt.datetime.fromisoformat(
                values["expires_at"]
            )
        return cls(**values)


@dataclass
class ManagedOrder:
    """管理器中的一个 attempt。"""

    local_order_id: str
    intent_id: str
    idempotency_key: str
    attempt_no: int
    client_order_id: str
    symbol: str
    side: OrderSide
    requested_quantity: int
    limit_price: float
    hard_limit_price: float | None = None
    broker_order_id: str | None = None
    status: ExecutionStatus = ExecutionStatus.CREATED
    filled_quantity: int = 0
    average_fill_price: float = 0.0
    chase_steps: int = 0
    submitted_at: dt.datetime | None = None
    cancel_requested_at: dt.datetime | None = None
    last_event_at: dt.datetime | None = None
    terminal_at: dt.datetime | None = None
    last_error: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def remaining_quantity(self) -> int:
        return max(0, self.requested_quantity - self.filled_quantity)

    @property
    def is_terminal(self) -> bool:
        return self.status.terminal

    def to_dict(self) -> dict:
        data = asdict(self)
        data["side"] = self.side.value
        data["status"] = self.status.value
        for key in (
            "submitted_at", "cancel_requested_at",
            "last_event_at", "terminal_at",
        ):
            value = getattr(self, key)
            data[key] = value.isoformat() if value else None
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ManagedOrder":
        values = dict(data)
        values["side"] = OrderSide(values["side"])
        values["status"] = ExecutionStatus(values["status"])
        for key in (
            "submitted_at", "cancel_requested_at",
            "last_event_at", "terminal_at",
        ):
            if values.get(key):
                values[key] = dt.datetime.fromisoformat(values[key])
        return cls(**values)


@dataclass(frozen=True)
class ExecutionEvent:
    event_id: str
    local_order_id: str
    event_type: str
    from_status: str | None
    to_status: str | None
    created_at: dt.datetime
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            **self.__dict__,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class ExecutionResult:
    order: ManagedOrder | None
    risk_decision: Any = None
    submitted: bool = False
    reused_existing: bool = False
    message: str = ""


def execution_status_from_broker(status: OrderStatus) -> ExecutionStatus:
    mapping = {
        OrderStatus.PENDING: ExecutionStatus.SUBMITTED,
        OrderStatus.SUBMITTED: ExecutionStatus.SUBMITTED,
        OrderStatus.PARTIALLY_FILLED: ExecutionStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED: ExecutionStatus.FILLED,
        OrderStatus.CANCEL_PENDING: ExecutionStatus.CANCEL_PENDING,
        OrderStatus.CANCELLED: ExecutionStatus.CANCELLED,
        OrderStatus.REJECTED: ExecutionStatus.REJECTED,
        OrderStatus.EXPIRED: ExecutionStatus.EXPIRED,
        OrderStatus.UNKNOWN: ExecutionStatus.UNKNOWN,
    }
    return mapping.get(status, ExecutionStatus.UNKNOWN)
