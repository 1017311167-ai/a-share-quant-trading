"""交易前、交易中和账户级风险引擎。"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import replace

from broker_adapter.base_broker import BaseBroker
from broker_adapter.models import OrderRequest, OrderSide
from risk.models import (
    AccountState,
    PositionState,
    QuoteState,
    RiskContext,
    RiskDecision,
    RiskEvent,
    RiskLevel,
    RiskLimits,
    TradingState,
    approved_decision,
    rejected_decision,
)
from utils.config import get_price_limit


STATE_SEVERITY = {
    TradingState.ACTIVE: 0,
    TradingState.STOP_OPEN: 1,
    TradingState.REDUCE_ONLY: 2,
    TradingState.KILLED: 3,
}


class RiskEngine:
    """订单执行前的强制风险闸门和账户风险监控。"""

    def __init__(self, limits: RiskLimits | None = None, *,
                 event_sink=None):
        self.limits = limits or RiskLimits()
        self.event_sink = event_sink
        self.state = TradingState.ACTIVE
        self.state_reason = ""
        self.connected = True
        self.connection_error = ""
        self.last_persistence_error = ""
        self._day = None
        self._start_equity = None
        self._peak_equity = None
        self.daily_return = 0.0
        self.current_drawdown = 0.0
        self.orders_today = 0
        self.events: list[RiskEvent] = []
        self._quotes: dict[str, QuoteState] = {}

    @property
    def policy_hash(self) -> str:
        return self.limits.policy_hash

    def snapshot(self) -> dict:
        return {
            "state": self.state.value,
            "state_reason": self.state_reason,
            "connected": self.connected,
            "last_persistence_error": self.last_persistence_error,
            "daily_return": self.daily_return,
            "current_drawdown": self.current_drawdown,
            "orders_today": self.orders_today,
            "policy_hash": self.policy_hash,
            "limits": self.limits.__dict__,
        }

    def get_events(self) -> list[dict]:
        return [item.to_dict() for item in self.events]

    def update_quote(self, quote: QuoteState):
        self._quotes[quote.symbol] = quote

    def update_quotes(self, quotes):
        for quote in quotes:
            self.update_quote(quote)

    def on_connection_status(self, connected: bool, error: str = "",
                             now=None):
        previous = self.connected
        self.connected = bool(connected)
        self.connection_error = str(error or "")
        if connected:
            if not previous:
                self._emit(
                    "CONNECTION_RESTORED",
                    RiskLevel.INFO,
                    "stop_open",
                    "resolved",
                    "券商连接已恢复，等待重新对账",
                )
            return
        if previous is False:
            return
        self._raise_state(
            TradingState.STOP_OPEN,
            rule_code="BROKER_DISCONNECTED",
            message=error or "券商连接异常，停止开仓",
            level=RiskLevel.CRITICAL,
        )

    def on_account_snapshot(self, account: AccountState, now=None):
        now = now or dt.datetime.now()
        day = now.date()
        if self._day != day:
            self._day = day
            self._start_equity = float(account.total_asset)
            if self._peak_equity is None:
                self._peak_equity = float(account.total_asset)
            self.orders_today = 0
            self.daily_return = 0.0
            self.current_drawdown = 0.0
        elif self._start_equity is None:
            self._start_equity = float(account.total_asset)
            self._peak_equity = float(account.total_asset)

        equity = float(account.total_asset)
        if self._peak_equity is None:
            self._peak_equity = equity
        self._peak_equity = max(self._peak_equity, equity)
        self.daily_return = (
            equity / self._start_equity - 1
            if self._start_equity else 0.0
        )
        self.current_drawdown = (
            1 - equity / self._peak_equity
            if self._peak_equity else 0.0
        )
        if self.daily_return <= -self.limits.max_daily_loss_pct:
            self._raise_state(
                TradingState.STOP_OPEN,
                rule_code="DAILY_LOSS_LIMIT",
                message=(
                    f"单日亏损 {abs(self.daily_return):.2%} "
                    f"达到限额 {self.limits.max_daily_loss_pct:.2%}"
                ),
                level=RiskLevel.CRITICAL,
                measured_value=abs(self.daily_return),
                threshold_value=self.limits.max_daily_loss_pct,
            )
        if self.current_drawdown >= self.limits.max_drawdown_pct:
            self._raise_state(
                TradingState.REDUCE_ONLY,
                rule_code="MAX_DRAWDOWN_LIMIT",
                message=(
                    f"当前回撤 {self.current_drawdown:.2%} "
                    f"达到限额 {self.limits.max_drawdown_pct:.2%}"
                ),
                level=RiskLevel.CRITICAL,
                measured_value=self.current_drawdown,
                threshold_value=self.limits.max_drawdown_pct,
            )

    def enable_stop_open(self, reason: str):
        self._raise_state(
            TradingState.STOP_OPEN,
            "STOP_OPEN",
            reason,
            RiskLevel.WARNING,
        )

    def enable_reduce_only(self, reason: str):
        self._raise_state(
            TradingState.REDUCE_ONLY,
            "REDUCE_ONLY",
            reason,
            RiskLevel.WARNING,
        )

    def enable_kill_switch(self, reason: str):
        self._raise_state(
            TradingState.KILLED,
            "GLOBAL_KILL_SWITCH",
            reason,
            RiskLevel.CRITICAL,
        )

    def recover(self, *, state: TradingState = TradingState.ACTIVE,
                reason: str = "manual recovery"):
        """人工恢复风险状态。"""
        if not isinstance(state, TradingState):
            state = TradingState(state)
        self.state = state
        self.state_reason = reason
        self._emit(
            "MANUAL_RECOVERY",
            RiskLevel.WARNING,
            state.value,
            "resolved",
            reason,
        )

    def check_runtime(self, context: RiskContext) -> list[RiskEvent]:
        """更新连接、账户和行情状态，返回本次产生的事件。"""
        before = len(self.events)
        self.on_connection_status(
            context.connected, self.connection_error, now=context.now
        )
        self.on_account_snapshot(context.account, now=context.now)
        for symbol, quote in context.quotes.items():
            self.update_quote(quote)
            age = (context.now - quote.timestamp).total_seconds()
            if age > self.limits.max_quote_age_seconds:
                self._emit(
                    "STALE_MARKET_DATA",
                    RiskLevel.WARNING,
                    "stop_symbol",
                    "triggered",
                    f"{symbol} 行情已过期 {age:.1f} 秒",
                    symbol=symbol,
                    measured_value=age,
                    threshold_value=self.limits.max_quote_age_seconds,
                )
        return self.events[before:]

    def pre_trade_check(self, request: OrderRequest,
                        context: RiskContext) -> RiskDecision:
        """执行交易前和账户级检查，返回批准、缩量或拒绝。"""
        self.check_runtime(context)
        if self.state is TradingState.KILLED:
            return self._reject(
                request,
                ["GLOBAL_KILL_SWITCH"],
                [self.state_reason or "全局急停已启用"],
            )
        if self.orders_today >= self.limits.max_orders_per_day:
            return self._reject(
                request,
                ["MAX_ORDERS_PER_DAY"],
                [f"当日订单数已达上限 {self.limits.max_orders_per_day}"],
            )
        if not context.connected:
            return self._reject(
                request,
                ["BROKER_DISCONNECTED"],
                [self.connection_error or "券商连接异常"],
            )
        quote = context.quotes.get(request.symbol) or self._quotes.get(
            request.symbol
        )
        if quote is None:
            return self._reject(
                request,
                ["MISSING_MARKET_DATA"],
                [f"缺少 {request.symbol} 实时行情"],
            )
        age = (context.now - quote.timestamp).total_seconds()
        if age > self.limits.max_quote_age_seconds:
            return self._reject(
                request,
                ["STALE_MARKET_DATA"],
                [f"{request.symbol} 行情已过期 {age:.1f} 秒"],
            )
        if not quote.tradable:
            return self._reject(
                request,
                ["SUSPENDED"],
                [f"{request.symbol} 当前不可交易或停牌"],
            )
        if request.side is OrderSide.SELL:
            return self._check_sell(request, context, quote)
        if self.state is not TradingState.ACTIVE:
            return self._reject(
                request,
                ["ENTRY_BLOCKED"],
                [self.state_reason or "当前状态禁止开仓"],
            )
        return self._check_buy(request, context, quote)

    def record_order_submitted(self):
        self.orders_today += 1
        self._emit(
            "ORDER_ACCEPTED",
            RiskLevel.INFO,
            "allow",
            "noted",
            f"当日订单数更新为 {self.orders_today}",
            measured_value=self.orders_today,
            threshold_value=self.limits.max_orders_per_day,
        )

    def _check_sell(self, request, context, quote) -> RiskDecision:
        if not self.state.exits_allowed:
            return self._reject(
                request,
                ["EXIT_BLOCKED"],
                [self.state_reason or "当前状态禁止卖出"],
            )
        position = context.position_map().get(request.symbol)
        available = position.available_quantity if position else 0
        if available <= 0:
            return self._reject(
                request,
                ["T_PLUS_1_OR_NO_POSITION"],
                [f"{request.symbol} 没有可卖持仓"],
            )
        limit_up, limit_down = _effective_limits(quote, request.symbol)
        if _below_limit(request.price, limit_down,
                        self.limits.price_limit_tolerance):
            return self._reject(
                request,
                ["PRICE_LIMIT_DOWN"],
                [f"卖出价格 {request.price} 低于跌停价 {limit_down}"],
            )
        if request.quantity > available:
            reason = (
                f"{request.symbol} 卖出数量超过可用持仓，"
                f"从 {request.quantity} 缩量到 {available}"
            )
            self._emit(
                "T_PLUS_1_OR_POSITION_RESIZED",
                RiskLevel.WARNING,
                "resize",
                "triggered",
                reason,
                symbol=request.symbol,
                measured_value=request.quantity,
                threshold_value=available,
            )
            return RiskDecision(
                status="resized",
                approved_request=replace(request, quantity=available),
                approved_quantity=available,
                rule_codes=("T_PLUS_1_OR_POSITION_RESIZED",),
                reasons=(reason,),
                trading_state=self.state,
            )
        return approved_decision(request, state=self.state)

    def _check_buy(self, request, context, quote) -> RiskDecision:
        if request.quantity % self.limits.lot_size:
            return self._reject(
                request,
                ["LOT_SIZE"],
                [f"买入数量必须是 {self.limits.lot_size} 股的整数倍"],
            )
        limit_up, limit_down = _effective_limits(quote, request.symbol)
        if _above_limit(request.price, limit_up,
                        self.limits.price_limit_tolerance):
            return self._reject(
                request,
                ["PRICE_LIMIT_UP"],
                [f"买入价格 {request.price} 高于涨停价 {limit_up}"],
            )
        total_asset = context.account.total_asset
        if total_asset <= 0:
            return self._reject(
                request, ["INVALID_TOTAL_ASSET"], ["账户总资产无效"]
            )
        positions = context.position_map()
        current_value = _position_value(positions, quote, request.symbol)
        gross_value = _gross_exposure(positions, context.quotes)
        max_single_cash = max(
            0.0,
            self.limits.max_single_position_pct * total_asset - current_value,
        )
        max_gross_cash = max(
            0.0,
            self.limits.max_gross_exposure_pct * total_asset - gross_value,
        )
        min_cash_cash = max(
            0.0,
            context.account.available_cash
            - self.limits.min_cash_pct * total_asset,
        )
        max_cash = min(
            context.account.available_cash,
            max_single_cash,
            max_gross_cash,
            min_cash_cash,
        )
        if max_cash <= 0:
            return self._reject(
                request,
                ["INSUFFICIENT_RISK_BUDGET"],
                ["现金、单票仓位或总仓位约束不允许继续买入"],
            )
        max_quantity = int(
            max_cash / max(request.price, 1e-9)
        )
        max_quantity = (
            max_quantity // self.limits.lot_size * self.limits.lot_size
        )
        if max_quantity <= 0:
            return self._reject(
                request,
                ["INSUFFICIENT_CASH"],
                ["可用风险资金不足以买入一手"],
            )
        approved_quantity = min(request.quantity, max_quantity)
        if approved_quantity < request.quantity:
            if max_cash == max_single_cash:
                rule_code = "SINGLE_POSITION_LIMIT"
            elif max_cash == max_gross_cash:
                rule_code = "GROSS_EXPOSURE_LIMIT"
            elif max_cash == min_cash_cash:
                rule_code = "MIN_CASH_LIMIT"
            else:
                rule_code = "CASH_LIMIT"
            reason = (
                f"买入数量从 {request.quantity} 缩量到 "
                f"{approved_quantity}，触发 {rule_code}"
            )
            self._emit(
                rule_code,
                RiskLevel.WARNING,
                "resize",
                "triggered",
                reason,
                symbol=request.symbol,
                measured_value=request.quantity,
                threshold_value=approved_quantity,
            )
            return RiskDecision(
                status="resized",
                approved_request=replace(
                    request, quantity=approved_quantity
                ),
                approved_quantity=approved_quantity,
                rule_codes=(rule_code,),
                reasons=(reason,),
                trading_state=self.state,
            )
        return approved_decision(
            request,
            state=self.state,
            quantity=approved_quantity,
        )

    def _raise_state(self, state: TradingState, rule_code: str,
                     message: str, level: RiskLevel,
                     measured_value=None, threshold_value=None):
        if STATE_SEVERITY[state] > STATE_SEVERITY[self.state]:
            self.state = state
            self.state_reason = message
        self._emit(
            rule_code,
            level,
            state.value,
            "triggered",
            message,
            measured_value=measured_value,
            threshold_value=threshold_value,
        )

    def _reject(self, request, codes, reasons):
        for code, reason in zip(codes, reasons):
            self._emit(
                str(code),
                RiskLevel.WARNING,
                "reject",
                "triggered",
                str(reason),
                symbol=request.symbol,
            )
        return rejected_decision(
            request, state=self.state, codes=codes, reasons=reasons
        )

    def _emit(self, rule_code, level, action, status, message, *,
              symbol=None, measured_value=None, threshold_value=None,
              details=None):
        event = RiskEvent(
            event_id=str(uuid.uuid4()),
            rule_code=rule_code,
            level=level,
            action=action,
            status=status,
            message=message,
            symbol=symbol,
            measured_value=measured_value,
            threshold_value=threshold_value,
            details=dict(details or {}),
        )
        self.events.append(event)
        if self.event_sink is not None:
            try:
                self.event_sink(event)
                self.last_persistence_error = ""
            except Exception as exc:
                # 持久化失败不能绕过内存风控，也不能伪装成券商断线。
                self.last_persistence_error = (
                    f"风险事件持久化失败：{type(exc).__name__}: {exc}"
                )


def risk_context_from_broker(
        broker: BaseBroker,
        quotes,
        *,
        connected: bool | None = None,
        now=None,
) -> RiskContext:
    """把 Broker 的资金、持仓和行情转换为 RiskContext。"""
    account = _account_from_dict(broker.get_account_info())
    positions = tuple(
        _position_from_dict(item) for item in broker.get_positions()
    )
    quote_map = {}
    if isinstance(quotes, dict):
        for symbol, item in quotes.items():
            quote = item if isinstance(item, QuoteState) else QuoteState(
                symbol=symbol, **item
            )
            quote_map[quote.symbol] = quote
    else:
        for item in quotes:
            quote = item if isinstance(item, QuoteState) else QuoteState(**item)
            quote_map[quote.symbol] = quote
    return RiskContext(
        account=account,
        positions=positions,
        quotes=quote_map,
        connected=broker._connected if connected is None else connected,
        now=now or dt.datetime.now(),
    )


def submit_with_risk(broker: BaseBroker, engine: RiskEngine,
                     request: OrderRequest, quotes, *, now=None):
    """风险检查通过后提交订单，返回 (decision, order_id)。"""
    context = risk_context_from_broker(broker, quotes, now=now)
    decision = engine.pre_trade_check(request, context)
    if not decision.approved:
        return decision, None
    order_id = broker.submit_order(decision.approved_request)
    engine.record_order_submitted()
    return decision, order_id


def execute_kill_switch(broker: BaseBroker, engine: RiskEngine,
                        reason: str) -> int:
    """启用全局急停并撤销券商全部可撤订单。"""
    engine.enable_kill_switch(reason)
    try:
        cancelled = broker.cancel_order_all()
    except Exception as exc:
        engine._emit(
            "KILL_SWITCH_CANCEL_FAILED",
            RiskLevel.CRITICAL,
            "killed",
            "failed",
            f"全局急停后撤单失败：{exc}",
        )
        raise
    engine._emit(
        "KILL_SWITCH_CANCEL_ALL",
        RiskLevel.CRITICAL,
        "killed",
        "completed",
        f"全局急停已发出 {cancelled} 笔撤单",
        measured_value=cancelled,
    )
    return int(cancelled)


def _account_from_dict(data: dict) -> AccountState:
    return AccountState(
        account_id=str(data.get("资金账号", "")),
        total_asset=float(data.get("总资产", 0.0)),
        available_cash=float(data.get("可用资金", 0.0)),
        frozen_cash=float(data.get("冻结资金", 0.0)),
        market_value=float(data.get("持仓市值", 0.0)),
    )


def _position_from_dict(data: dict) -> PositionState:
    return PositionState(
        symbol=str(data.get("股票代码", "")).split(".")[0],
        total_quantity=int(data.get("持仓数量", 0)),
        available_quantity=int(data.get("可用数量", 0)),
        frozen_quantity=int(data.get("冻结数量", 0)),
        market_value=float(data.get("市值", 0.0)),
        last_price=float(data.get("最新价", 0.0)),
        cost_price=float(data.get("成本价", 0.0)),
    )


def _position_value(positions, quote, symbol) -> float:
    position = positions.get(symbol)
    if position is None:
        return 0.0
    return position.total_quantity * quote.last_price


def _gross_exposure(positions, quotes) -> float:
    total = 0.0
    for symbol, position in positions.items():
        quote = quotes.get(symbol)
        price = quote.last_price if quote else position.last_price
        total += position.total_quantity * price
    return total


def _above_limit(price, limit, tolerance) -> bool:
    return limit is not None and price > float(limit) * (1 + tolerance)


def _below_limit(price, limit, tolerance) -> bool:
    return limit is not None and price < float(limit) * (1 - tolerance)


def _effective_limits(quote: QuoteState, symbol: str):
    if quote.limit_up is not None and quote.limit_down is not None:
        return float(quote.limit_up), float(quote.limit_down)
    if quote.previous_close is None:
        return quote.limit_up, quote.limit_down
    pct = get_price_limit(symbol)
    return (
        round(float(quote.previous_close) * (1 + pct), 2),
        round(float(quote.previous_close) * (1 - pct), 2),
    )
