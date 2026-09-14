"""内存 MockBroker，用于模拟盘和交易状态机测试。"""

from __future__ import annotations

import datetime as dt

from broker_adapter.base_broker import (
    BaseBroker,
    BrokerConnectionError,
    BrokerDataError,
    BrokerOrderError,
)
from broker_adapter.models import (
    BrokerEvent,
    OrderRequest,
    OrderSide,
    OrderSnapshot,
    OrderStatus,
    TradeSnapshot,
    coerce_datetime,
    normalize_order_status,
    normalize_symbol,
)


class MockBroker(BaseBroker):
    """确定性的内存券商，不连接外部系统。"""

    def __init__(
            self,
            *,
            account_id="MOCK-001",
            initial_cash=1_000_000.0,
            positions=None,
            fill_mode="none",
            partial_fill_ratio=0.5,
            commission=0.00025,
            commission_min=5.0,
            stamp_tax=True,
            transfer_fee=True,
            t_plus_one=False,
            on_event=None,
    ):
        if fill_mode not in {"none", "full", "partial", "reject"}:
            raise ValueError("fill_mode 可选 none/full/partial/reject")
        self.account_id = account_id
        self.initial_cash = float(initial_cash)
        self.cash = float(initial_cash)
        self.fill_mode = fill_mode
        self.partial_fill_ratio = float(partial_fill_ratio)
        self.commission = float(commission)
        self.commission_min = float(commission_min)
        self.stamp_tax = bool(stamp_tax)
        self.transfer_fee = bool(transfer_fee)
        self.t_plus_one = bool(t_plus_one)
        self.on_event = on_event
        self._connected = False
        self.is_simulation = True
        self.trading_mode = "SIMULATION"
        self._orders: dict[str, dict] = {}
        self._trades: list[TradeSnapshot] = []
        self._positions = {}
        for symbol, value in (positions or {}).items():
            if isinstance(value, dict):
                quantity = int(value.get("quantity", 0))
                cost = float(value.get("cost", 0.0))
            else:
                quantity = int(value)
                cost = 0.0
            self._positions[normalize_symbol(symbol)] = {
                "quantity": quantity,
                "available": quantity,
                "frozen": 0,
                "cost": cost,
                "last": cost,
            }
        self._order_seq = 0
        self._trade_seq = 0
        self._subscribed = {}
        self.calls = []

    def connect(self):
        if self._connected:
            return
        self._connected = True
        self.calls.append(("connect",))
        self._emit("connected", BrokerEvent(
            event_type="connected", account_id=self.account_id,
        ))

    def disconnect(self):
        self._connected = False
        self.calls.append(("disconnect",))
        self._emit("disconnected", BrokerEvent(
            event_type="disconnected", account_id=self.account_id,
        ))

    def simulate_disconnect(self):
        """模拟连接意外中断。"""
        self._connected = False
        self._emit("disconnected", BrokerEvent(
            event_type="disconnected",
            account_id=self.account_id,
            error_message="simulated disconnect",
        ))

    def _ensure_connected(self):
        if not self._connected:
            raise BrokerConnectionError("MockBroker 尚未连接")

    def submit_order(self, request: OrderRequest) -> int:
        self._ensure_connected()
        self.calls.append(("submit_order", request))
        if request.quantity % 100 and request.side is OrderSide.BUY:
            raise BrokerOrderError(
                f"买入数量必须为 100 股整数倍：{request.quantity}"
            )
        if self.fill_mode == "reject":
            self._order_seq += 1
            order_id = self._order_seq
            self._orders[str(order_id)] = {
                "broker_order_id": str(order_id),
                "symbol": normalize_symbol(request.symbol),
                "side": request.side,
                "price": request.price,
                "quantity": request.quantity,
                "filled_quantity": 0,
                "average_fill_price": 0.0,
                "status": OrderStatus.REJECTED,
                "account_id": self.account_id,
                "client_order_id": request.client_order_id,
                "order_type": request.order_type,
                "submitted_at": dt.datetime.now(),
                "updated_at": dt.datetime.now(),
                "status_message": "mock rejected",
                "strategy_name": request.strategy_name,
                "order_remark": request.remark,
                "raw": {"mock": True},
            }
            self._emit_order(order_id)
            raise BrokerOrderError(
                "模拟废单", order_id=order_id, error_code=15
            )
        self._order_seq += 1
        order_id = self._order_seq
        now = dt.datetime.now()
        self._orders[str(order_id)] = {
            "broker_order_id": str(order_id),
            "symbol": normalize_symbol(request.symbol),
            "side": request.side,
            "price": request.price,
            "quantity": request.quantity,
            "filled_quantity": 0,
            "average_fill_price": 0.0,
            "status": OrderStatus.SUBMITTED,
            "account_id": self.account_id,
            "client_order_id": request.client_order_id,
            "order_type": request.order_type,
            "submitted_at": now,
            "updated_at": now,
            "status_message": "mock submitted",
            "strategy_name": request.strategy_name,
            "order_remark": request.remark,
            "raw": {"mock": True},
        }
        self._emit_order(order_id)
        if self.fill_mode == "full":
            self.fill_order(order_id, request.quantity, request.price)
        elif self.fill_mode == "partial":
            quantity = int(request.quantity * self.partial_fill_ratio / 100) * 100
            if quantity > 0:
                self.fill_order(order_id, quantity, request.price)
        return order_id

    def order_buy(self, code, price, volume):
        return self.submit_order(OrderRequest(
            symbol=code,
            side=OrderSide.BUY,
            price=price,
            quantity=volume,
        ))

    def order_sell(self, code, price, volume):
        return self.submit_order(OrderRequest(
            symbol=code,
            side=OrderSide.SELL,
            price=price,
            quantity=volume,
        ))

    def cancel_order(self, order_id) -> bool:
        self._ensure_connected()
        order = self._get_order_record(order_id)
        if order["status"].terminal:
            self.calls.append(("cancel_order_terminal", str(order_id)))
            return False
        self.calls.append(("cancel_order", str(order_id)))
        order["status"] = OrderStatus.CANCELLED
        order["updated_at"] = dt.datetime.now()
        order["status_message"] = "mock cancelled"
        self._emit_order(order_id)
        return True

    def cancel_order_all(self) -> int:
        self._ensure_connected()
        count = 0
        for order in list(self._orders.values()):
            if not order["status"].terminal:
                if self.cancel_order(order["broker_order_id"]):
                    count += 1
        return count

    def get_order(self, order_id) -> OrderSnapshot:
        self._ensure_connected()
        return self._snapshot(self._get_order_record(order_id))

    def get_orders(self, *, cancelable_only: bool = False,
                   symbol: str | None = None, status=None,
                   start_time=None, end_time=None) -> list[OrderSnapshot]:
        self._ensure_connected()
        result = []
        for order in self._orders.values():
            if cancelable_only and order["status"].terminal:
                continue
            if symbol is not None and order["symbol"] != normalize_symbol(symbol):
                continue
            if status is not None and order["status"] != normalize_order_status(status):
                continue
            if not _time_matches(order["updated_at"], start_time, end_time):
                continue
            result.append(self._snapshot(order))
        return sorted(
            result, key=lambda item: item.submitted_at or dt.datetime.min
        )

    def get_trades(self, *, order_id=None, symbol: str | None = None,
                   start_time=None, end_time=None) -> list[TradeSnapshot]:
        self._ensure_connected()
        result = []
        for trade in self._trades:
            if order_id is not None and str(trade.broker_order_id) != str(order_id):
                continue
            if symbol is not None and trade.symbol != normalize_symbol(symbol):
                continue
            if not _time_matches(trade.traded_at, start_time, end_time):
                continue
            result.append(trade)
        return result

    def fill_order(self, order_id, quantity=None, price=None,
                   commission=None, stamp_tax=None, transfer_fee=None):
        """手工触发成交，便于测试部分成交和回调。"""
        self._ensure_connected()
        order = self._get_order_record(order_id)
        if order["status"].terminal:
            raise BrokerOrderError(
                f"订单已处于终态，不能成交：{order_id}",
                order_id=order_id,
            )
        remaining = order["quantity"] - order["filled_quantity"]
        quantity = int(quantity or remaining)
        if quantity <= 0 or quantity > remaining:
            raise BrokerOrderError(
                f"成交数量非法：{quantity}，剩余 {remaining}",
                order_id=order_id,
            )
        price = float(price if price is not None else order["price"])
        gross = quantity * price
        fees = self._fees(order["side"], gross, commission,
                         stamp_tax, transfer_fee)
        if order["side"] is OrderSide.BUY:
            required = gross + sum(fees.values())
            if required > self.cash + 1e-9:
                order["status"] = OrderStatus.REJECTED
                order["status_message"] = "mock insufficient cash"
                order["updated_at"] = dt.datetime.now()
                self._emit_order(order_id)
                raise BrokerOrderError(
                    "模拟资金不足", order_id=order_id, error_code=3
                )
            self.cash -= required
            position = self._positions.setdefault(order["symbol"], {
                "quantity": 0, "available": 0, "frozen": 0,
                "cost": 0.0, "last": price,
            })
            old_qty = position["quantity"]
            new_qty = old_qty + quantity
            position["cost"] = (
                position["cost"] * old_qty + gross + sum(fees.values())
            ) / new_qty
            position["quantity"] = new_qty
            if not self.t_plus_one:
                position["available"] += quantity
            position["last"] = price
        else:
            position = self._positions.get(order["symbol"])
            if position is None or position["available"] < quantity:
                raise BrokerOrderError(
                    "模拟持仓不足", order_id=order_id, error_code=4
                )
            self.cash += gross - sum(fees.values())
            position["quantity"] -= quantity
            position["available"] -= quantity
            position["last"] = price
        self._trade_seq += 1
        trade = TradeSnapshot(
            trade_id=f"MOCK-TRADE-{self._trade_seq}",
            broker_order_id=str(order_id),
            symbol=order["symbol"],
            side=order["side"],
            quantity=quantity,
            price=price,
            amount=gross,
            traded_at=dt.datetime.now(),
            account_id=self.account_id,
            client_order_id=order["client_order_id"],
            commission=fees["commission"],
            stamp_tax=fees["stamp_tax"],
            transfer_fee=fees["transfer_fee"],
            total_fee=sum(fees.values()),
            direction=order["side"].value,
            raw={"mock": True},
        )
        self._trades.append(trade)
        order["filled_quantity"] += quantity
        order["average_fill_price"] = (
            order["average_fill_price"] * (order["filled_quantity"] - quantity)
            + gross
        ) / order["filled_quantity"]
        order["status"] = (
            OrderStatus.FILLED
            if order["filled_quantity"] >= order["quantity"]
            else OrderStatus.PARTIALLY_FILLED
        )
        order["updated_at"] = trade.traded_at
        order["status_message"] = "mock filled"
        self._emit_order(order_id)
        self._emit("trade", BrokerEvent(
            event_type="trade",
            account_id=self.account_id,
            trade=trade,
        ))
        return trade

    def settle_positions(self):
        """测试用：执行日终 T+1 结算，使全部持仓变为可卖。"""
        for position in self._positions.values():
            position["available"] = position["quantity"]
        self.calls.append(("settle_positions",))

    def reject_order(self, order_id, message="mock rejected", error_code=15):
        order = self._get_order_record(order_id)
        if order["status"].terminal:
            return False
        order["status"] = OrderStatus.REJECTED
        order["status_message"] = message
        order["updated_at"] = dt.datetime.now()
        self._emit_order(order_id)
        self._emit("order_error", BrokerEvent(
            event_type="order_error",
            account_id=self.account_id,
            error_code=error_code,
            error_message=message,
            order=self._snapshot(order),
        ))
        return True

    def get_account_info(self):
        self._ensure_connected()
        market_value = sum(
            item["quantity"] * item["last"]
            for item in self._positions.values()
        )
        return {
            "资金账号": self.account_id,
            "总资产": self.cash + market_value,
            "可用资金": self.cash,
            "冻结资金": 0.0,
            "持仓市值": market_value,
            "可取资金": self.cash,
        }

    def get_positions(self):
        self._ensure_connected()
        return [{
            "股票代码": symbol,
            "股票名称": symbol,
            "持仓数量": item["quantity"],
            "可用数量": item["available"],
            "冻结数量": item["frozen"],
            "成本价": item["cost"],
            "最新价": item["last"],
            "市值": item["quantity"] * item["last"],
            "浮动盈亏比例": (
                item["last"] / item["cost"] - 1 if item["cost"] else 0.0
            ),
        } for symbol, item in self._positions.items()]

    def subscribe_realtime(self, codes, callback=None):
        self._ensure_connected()
        values = [codes] if isinstance(codes, str) else list(codes)
        for symbol in values:
            self._subscribed[normalize_symbol(symbol)] = callback
        return len(values)

    def push_quote(self, symbol, quote):
        callback = self._subscribed.get(normalize_symbol(symbol))
        if callback:
            callback(normalize_symbol(symbol), quote)

    def _get_order_record(self, order_id) -> dict:
        order = self._orders.get(str(order_id))
        if order is None:
            raise BrokerDataError(f"未找到订单：{order_id}")
        return order

    def _snapshot(self, order) -> OrderSnapshot:
        return OrderSnapshot(
            broker_order_id=order["broker_order_id"],
            symbol=order["symbol"],
            side=order["side"],
            price=order["price"],
            quantity=order["quantity"],
            filled_quantity=order["filled_quantity"],
            average_fill_price=order["average_fill_price"],
            status=order["status"],
            account_id=order["account_id"],
            client_order_id=order["client_order_id"],
            order_type=order["order_type"],
            submitted_at=order["submitted_at"],
            updated_at=order["updated_at"],
            status_message=order["status_message"],
            strategy_name=order["strategy_name"],
            order_remark=order["order_remark"],
            direction=order["side"].value,
            raw=dict(order["raw"]),
        )

    def _emit_order(self, order_id):
        self._emit("order", BrokerEvent(
            event_type="order",
            account_id=self.account_id,
            order=self._snapshot(self._get_order_record(order_id)),
        ))

    def _emit(self, event_type, event):
        if self.on_event:
            self.on_event(event_type, event.to_dict())

    def _fees(self, side, gross, commission, stamp_tax, transfer_fee):
        commission_rate = self.commission if commission is None else float(commission)
        payment = max(gross * commission_rate, self.commission_min)
        stamp = gross * 0.0005 if (
            side is OrderSide.SELL
            and (self.stamp_tax if stamp_tax is None else stamp_tax)
        ) else 0.0
        transfer = gross * 0.00001 if (
            self.transfer_fee if transfer_fee is None else transfer_fee
        ) else 0.0
        return {
            "commission": payment,
            "stamp_tax": stamp,
            "transfer_fee": transfer,
        }


def _time_matches(value, start_time, end_time):
    value = coerce_datetime(value)
    start = coerce_datetime(start_time)
    end = coerce_datetime(end_time)
    if value is None:
        return True
    if start is not None and value < start:
        return False
    if end is not None and value > end:
        return False
    return True
