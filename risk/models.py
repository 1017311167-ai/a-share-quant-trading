"""实盘交易风控模型。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from broker_adapter.models import OrderRequest


class TradingState(str, Enum):
    ACTIVE = "active"
    STOP_OPEN = "stop_open"
    REDUCE_ONLY = "reduce_only"
    KILLED = "killed"

    @property
    def entries_allowed(self) -> bool:
        return self is TradingState.ACTIVE

    @property
    def exits_allowed(self) -> bool:
        return self is not TradingState.KILLED


class RiskLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class RiskLimits:
    """账户和订单级风险阈值。"""

    version: str = "risk-v1"
    max_single_position_pct: float = 0.20
    max_gross_exposure_pct: float = 0.80
    min_cash_pct: float = 0.05
    max_daily_loss_pct: float = 0.02
    max_drawdown_pct: float = 0.08
    max_quote_age_seconds: float = 30.0
    max_orders_per_day: int = 50
    lot_size: int = 100
    price_limit_tolerance: float = 0.001

    def __post_init__(self):
        for name in (
            "max_single_position_pct",
            "max_gross_exposure_pct",
            "min_cash_pct",
            "max_daily_loss_pct",
            "max_drawdown_pct",
        ):
            value = float(getattr(self, name))
            if not 0 <= value <= 1:
                raise ValueError(f"{name} 必须在 [0, 1] 之间")
        if self.max_gross_exposure_pct + self.min_cash_pct > 1 + 1e-9:
            raise ValueError("最大总仓位与最低现金比例之和不能超过 1")
        if self.max_quote_age_seconds <= 0:
            raise ValueError("最大行情年龄必须大于 0")
        if self.max_orders_per_day < 1:
            raise ValueError("每日最大订单数必须为正整数")
        if self.lot_size < 1:
            raise ValueError("lot_size 必须为正整数")

    @property
    def policy_hash(self) -> str:
        import hashlib
        import json

        payload = json.dumps(self.__dict__, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class AccountState:
    total_asset: float
    available_cash: float
    frozen_cash: float = 0.0
    market_value: float = 0.0
    account_id: str = ""


@dataclass(frozen=True)
class PositionState:
    symbol: str
    total_quantity: int
    available_quantity: int
    frozen_quantity: int = 0
    market_value: float = 0.0
    last_price: float = 0.0
    cost_price: float = 0.0


@dataclass(frozen=True)
class QuoteState:
    symbol: str
    last_price: float
    previous_close: float | None = None
    limit_up: float | None = None
    limit_down: float | None = None
    timestamp: dt.datetime = field(default_factory=dt.datetime.now)
    tradable: bool = True


@dataclass(frozen=True)
class RiskContext:
    account: AccountState
    positions: tuple[PositionState, ...] = ()
    quotes: dict[str, QuoteState] = field(default_factory=dict)
    connected: bool = True
    now: dt.datetime = field(default_factory=dt.datetime.now)

    def position_map(self) -> dict[str, PositionState]:
        return {item.symbol: item for item in self.positions}


@dataclass(frozen=True)
class RiskDecision:
    status: str
    approved_request: OrderRequest | None
    approved_quantity: int
    rule_codes: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    trading_state: TradingState = TradingState.ACTIVE
    evaluated_at: dt.datetime = field(default_factory=dt.datetime.now)

    @property
    def approved(self) -> bool:
        return self.status in {"approved", "resized"}

    @property
    def resized(self) -> bool:
        return self.status == "resized"


@dataclass(frozen=True)
class RiskEvent:
    event_id: str
    rule_code: str
    level: RiskLevel
    action: str
    status: str
    message: str
    measured_value: float | None = None
    threshold_value: float | None = None
    symbol: str | None = None
    occurred_at: dt.datetime = field(default_factory=dt.datetime.now)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            **self.__dict__,
            "level": self.level.value,
            "occurred_at": self.occurred_at.isoformat(),
        }


def approved_decision(request: OrderRequest, *, state: TradingState,
                      quantity: int | None = None) -> RiskDecision:
    adjusted = request if quantity is None else replace(
        request, quantity=int(quantity)
    )
    status = "approved" if quantity is None or quantity == request.quantity \
        else "resized"
    return RiskDecision(
        status=status,
        approved_request=adjusted,
        approved_quantity=adjusted.quantity,
        trading_state=state,
    )


def rejected_decision(request: OrderRequest, *, state: TradingState,
                      codes, reasons) -> RiskDecision:
    return RiskDecision(
        status="rejected",
        approved_request=None,
        approved_quantity=0,
        rule_codes=tuple(codes),
        reasons=tuple(reasons),
        trading_state=state,
    )
