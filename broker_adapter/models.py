"""券商适配层的统一数据模型。"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"

    @property
    def terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }


@dataclass(frozen=True)
class OrderRequest:
    """统一下单请求。"""

    symbol: str
    side: OrderSide
    price: float
    quantity: int
    order_type: OrderType = OrderType.LIMIT
    client_order_id: str | None = None
    strategy_name: str = "quant"
    remark: str = ""

    def __post_init__(self):
        if isinstance(self.side, str):
            object.__setattr__(self, "side", OrderSide(self.side.lower()))
        if isinstance(self.order_type, str):
            object.__setattr__(
                self, "order_type", OrderType(self.order_type.lower())
            )
        if self.order_type is not OrderType.LIMIT:
            raise ValueError("首版统一交易接口只支持限价单")
        if float(self.price) <= 0:
            raise ValueError("委托价格必须大于 0")
        if int(self.quantity) <= 0:
            raise ValueError("委托数量必须大于 0")
        object.__setattr__(self, "price", float(self.price))
        object.__setattr__(self, "quantity", int(self.quantity))


@dataclass(frozen=True)
class OrderSnapshot:
    """统一订单快照。"""

    broker_order_id: str
    symbol: str
    side: OrderSide
    price: float
    quantity: int
    filled_quantity: int = 0
    average_fill_price: float = 0.0
    status: OrderStatus = OrderStatus.UNKNOWN
    account_id: str | None = None
    client_order_id: str | None = None
    order_type: OrderType = OrderType.LIMIT
    order_sys_id: str | None = None
    submitted_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None
    cancelled_quantity: int = 0
    status_message: str = ""
    strategy_name: str = ""
    order_remark: str = ""
    direction: str = ""
    offset_flag: str = ""
    raw_status: Any = None
    raw: dict = field(default_factory=dict)

    @property
    def remaining_quantity(self) -> int:
        return max(0, int(self.quantity) - int(self.filled_quantity))

    @property
    def is_terminal(self) -> bool:
        return self.status.terminal

    def to_dict(self) -> dict:
        return _serialize(asdict(self))


@dataclass(frozen=True)
class TradeSnapshot:
    """统一成交快照。"""

    trade_id: str
    broker_order_id: str
    symbol: str
    side: OrderSide
    quantity: int
    price: float
    amount: float
    traded_at: dt.datetime | None = None
    account_id: str | None = None
    client_order_id: str | None = None
    order_sys_id: str | None = None
    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0
    total_fee: float = 0.0
    direction: str = ""
    offset_flag: str = ""
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return _serialize(asdict(self))


@dataclass(frozen=True)
class OrderDetail:
    """订单详情及关联成交。"""

    order: OrderSnapshot
    trades: tuple[TradeSnapshot, ...] = ()

    def to_dict(self) -> dict:
        return {
            "order": self.order.to_dict(),
            "trades": [item.to_dict() for item in self.trades],
        }


@dataclass(frozen=True)
class BrokerEvent:
    """标准化券商回调事件。"""

    event_type: str
    account_id: str | None = None
    timestamp: dt.datetime = field(default_factory=dt.datetime.now)
    order: OrderSnapshot | None = None
    trade: TradeSnapshot | None = None
    error_code: Any = None
    error_message: str = ""
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "account_id": self.account_id,
            "timestamp": _serialize(self.timestamp),
            "order": self.order.to_dict() if self.order else None,
            "trade": self.trade.to_dict() if self.trade else None,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "raw": _serialize(self.raw),
        }


def normalize_order_status(value, *, sdk_constants=None) -> OrderStatus:
    """把 QMT 数字状态或通用字符串转换为统一订单状态。"""
    if isinstance(value, OrderStatus):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        aliases = {
            "pending": OrderStatus.PENDING,
            "submitted": OrderStatus.SUBMITTED,
            "partially_filled": OrderStatus.PARTIALLY_FILLED,
            "partial": OrderStatus.PARTIALLY_FILLED,
            "filled": OrderStatus.FILLED,
            "cancel_pending": OrderStatus.CANCEL_PENDING,
            "cancelled": OrderStatus.CANCELLED,
            "canceled": OrderStatus.CANCELLED,
            "rejected": OrderStatus.REJECTED,
            "junk": OrderStatus.REJECTED,
            "expired": OrderStatus.EXPIRED,
            "unknown": OrderStatus.UNKNOWN,
        }
        if text in aliases:
            return aliases[text]
    numeric = _as_int(value)
    if numeric is None:
        return OrderStatus.UNKNOWN
    if sdk_constants is not None:
        mapping = {
            getattr(sdk_constants, "ORDER_UNREPORTED", None): OrderStatus.PENDING,
            getattr(sdk_constants, "ORDER_WAIT_REPORTING", None): OrderStatus.PENDING,
            getattr(sdk_constants, "ORDER_REPORTED", None): OrderStatus.SUBMITTED,
            getattr(sdk_constants, "ORDER_REPORTED_CANCEL", None): OrderStatus.CANCEL_PENDING,
            getattr(sdk_constants, "ORDER_PARTSUCC_CANCEL", None): OrderStatus.PARTIALLY_FILLED,
            getattr(sdk_constants, "ORDER_PART_CANCEL", None): OrderStatus.CANCELLED,
            getattr(sdk_constants, "ORDER_CANCELED", None): OrderStatus.CANCELLED,
            getattr(sdk_constants, "ORDER_PART_SUCC", None): OrderStatus.PARTIALLY_FILLED,
            getattr(sdk_constants, "ORDER_SUCCEEDED", None): OrderStatus.FILLED,
            getattr(sdk_constants, "ORDER_JUNK", None): OrderStatus.REJECTED,
            getattr(sdk_constants, "ORDER_UNKNOWN", None): OrderStatus.UNKNOWN,
        }
        if numeric in mapping and mapping[numeric] is not None:
            return mapping[numeric]
    fallback = {
        48: OrderStatus.PENDING,
        49: OrderStatus.PENDING,
        50: OrderStatus.SUBMITTED,
        51: OrderStatus.CANCEL_PENDING,
        52: OrderStatus.PARTIALLY_FILLED,
        53: OrderStatus.CANCELLED,
        54: OrderStatus.CANCELLED,
        55: OrderStatus.PARTIALLY_FILLED,
        56: OrderStatus.FILLED,
        57: OrderStatus.REJECTED,
        255: OrderStatus.UNKNOWN,
    }
    return fallback.get(numeric, OrderStatus.UNKNOWN)


def normalize_order_side(value, *, sdk_constants=None) -> OrderSide:
    """把 QMT 方向数字或通用字符串转换为统一下单方向。"""
    if isinstance(value, OrderSide):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"buy", "stock_buy", "买入", "23"}:
            return OrderSide.BUY
        if text in {"sell", "stock_sell", "卖出", "24"}:
            return OrderSide.SELL
    numeric = _as_int(value)
    if sdk_constants is not None:
        if numeric == getattr(sdk_constants, "STOCK_BUY", None):
            return OrderSide.BUY
        if numeric == getattr(sdk_constants, "STOCK_SELL", None):
            return OrderSide.SELL
    if numeric == 23:
        return OrderSide.BUY
    if numeric == 24:
        return OrderSide.SELL
    raise ValueError(f"未知委托方向：{value!r}")


def normalize_symbol(value) -> str:
    """去掉交易所后缀并统一为 6 位股票代码。"""
    text = str(value or "").strip().upper()
    if "." in text:
        text = text.split(".", 1)[0]
    return text


def coerce_datetime(value) -> dt.datetime | None:
    """把秒、毫秒时间戳或日期字符串转换为 datetime。"""
    if value in (None, ""):
        return None
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
        return dt.datetime.fromisoformat(str(value))
    except ValueError:
        pass
    text = str(value)
    for fmt in (
        "%Y%m%d%H%M%S",
        "%Y%m%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
    ):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _serialize(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    return value


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
