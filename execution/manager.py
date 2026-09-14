"""订单执行管理器。"""

from __future__ import annotations

import datetime as dt
import uuid

from broker_adapter.base_broker import (
    BaseBroker,
    BrokerConnectionError,
    BrokerDataError,
    BrokerError,
    BrokerOrderError,
    BrokerOrderUnknownError,
)
from broker_adapter.models import OrderRequest, OrderSide, OrderStatus
from execution.models import (
    ExecutionEvent,
    ExecutionPolicy,
    ExecutionResult,
    ExecutionStatus,
    ManagedOrder,
    OrderIntent,
    execution_status_from_broker,
)
from execution.store import SQLiteExecutionStore
from risk.models import RiskContext, RiskDecision
from trading.session import TradingSessionClosed, TradingSessionGuard


class OrderExecutionManager:
    """管理订单幂等、提交、查询、撤单、追价和恢复。"""

    def __init__(
            self,
            broker: BaseBroker,
            store: SQLiteExecutionStore,
            *,
            policy: ExecutionPolicy | None = None,
            risk_engine=None,
            risk_context_provider=None,
            price_provider=None,
            clock=None,
            require_risk: bool = False,
            session_guard: TradingSessionGuard | None = None,
    ):
        self.broker = broker
        self.store = store
        self.policy = policy or ExecutionPolicy()
        self.risk_engine = risk_engine
        self.risk_context_provider = risk_context_provider
        self.price_provider = price_provider
        self.clock = clock or dt.datetime.now
        self.require_risk = bool(require_risk)
        self.session_guard = session_guard or TradingSessionGuard()

    def submit_intent(
            self,
            intent: OrderIntent,
            *,
            market_price: float | None = None,
            risk_context: RiskContext | None = None,
    ) -> ExecutionResult:
        """幂等提交业务意图。"""
        if self.require_risk and self.risk_engine is None:
            raise RuntimeError("该执行通道强制要求 RiskEngine，禁止绕过风控")
        self.session_guard.require_order(
            intent.side, now=self.clock()
        )
        intent = self.store.save_intent(intent)
        existing = self.store.active_order_for_intent(intent.intent_id)
        if existing is not None:
            return ExecutionResult(
                order=existing,
                reused_existing=True,
                message="意图已有活动订单，返回已有订单",
            )

        now = self.clock()
        if intent.expires_at is not None and intent.expires_at <= now:
            self.store.update_intent_status(
                intent.idempotency_key, ExecutionStatus.EXPIRED.value
            )
            return ExecutionResult(
                order=None,
                message="订单意图已过期",
                risk_decision=None,
            )

        risk_decision = None
        approved_quantity = intent.quantity
        limit_price = intent.limit_price
        if self.risk_engine is not None:
            context = risk_context or (
                self.risk_context_provider()
                if self.risk_context_provider else None
            )
            if context is None:
                raise ValueError("配置了 RiskEngine 时必须提供 RiskContext")
            request = OrderRequest(
                symbol=intent.symbol,
                side=intent.side,
                price=limit_price,
                quantity=approved_quantity,
                client_order_id=f"{intent.idempotency_key}:1",
                strategy_name=str(intent.metadata.get("strategy_name", "quant")),
            )
            risk_decision = self.risk_engine.pre_trade_check(request, context)
            if not risk_decision.approved:
                return ExecutionResult(
                    order=None,
                    risk_decision=risk_decision,
                    message="风险检查拒绝：" + "；".join(risk_decision.reasons),
                )
            approved_quantity = risk_decision.approved_quantity
            limit_price = risk_decision.approved_request.price

        orders = self.store.orders_for_intent(intent.intent_id)
        attempt_no = len(orders) + 1
        if attempt_no > self.policy.max_attempts:
            return ExecutionResult(order=None, message="已达到最大尝试次数")
        order = self._new_order(
            intent,
            attempt_no=attempt_no,
            quantity=approved_quantity,
            limit_price=limit_price,
            chase_steps=0,
        )
        persisted = self.store.create_order(order)
        if persisted.local_order_id != order.local_order_id:
            return ExecutionResult(
                order=persisted,
                risk_decision=risk_decision,
                reused_existing=True,
                message="幂等键已存在，返回已有订单",
            )
        order = persisted
        self._record_event(order, "INTENT_PERSISTED")
        self._submit(order, market_price=market_price)
        latest = self.store.orders_for_intent(intent.intent_id)[-1]
        return ExecutionResult(
            order=latest,
            risk_decision=risk_decision,
            submitted=latest.broker_order_id is not None,
            message="订单已提交" if latest.broker_order_id else latest.last_error,
        )

    def process_order(self, local_order_id: str, *,
                      market_price: float | None = None,
                      now=None) -> ManagedOrder:
        """查询并推进单个订单。"""
        now = now or self.clock()
        order = self.store.get_order(local_order_id)
        if order.is_terminal:
            self._ingest_trades(order)
            return order
        try:
            self.session_guard.require_open(
                action="recover", now=now
            )
        except TradingSessionClosed as exc:
            order.last_error = str(exc)
            self.store.save_order(order)
            self._record_event(
                order,
                "PROCESS_BLOCKED_BY_SESSION",
                details={"reason": str(exc)},
            )
            return self.store.get_order(local_order_id)
        broker_order = self._find_broker_order(order)
        if broker_order is not None:
            self._ingest_trades(order)
            previous = order.status
            order.broker_order_id = broker_order.broker_order_id
            order.filled_quantity = broker_order.filled_quantity
            order.average_fill_price = broker_order.average_fill_price
            broker_status = execution_status_from_broker(broker_order.status)
            if (
                previous is ExecutionStatus.CANCEL_PENDING
                and not broker_status.terminal
            ):
                order.status = ExecutionStatus.CANCEL_PENDING
            else:
                order.status = broker_status
            order.last_event_at = now
            if order.status is ExecutionStatus.FILLED:
                self._transition(
                    order, ExecutionStatus.FILLED, "BROKER_FILLED",
                    {"broker_status": broker_order.status.value},
                    now=now,
                )
                self.store.update_intent_status(
                    order.idempotency_key, ExecutionStatus.FILLED.value
                )
                return self.store.get_order(order.local_order_id)
            if order.status is ExecutionStatus.REJECTED:
                self._transition(
                    order, ExecutionStatus.REJECTED, "BROKER_REJECTED",
                    {"message": broker_order.status_message},
                    now=now,
                )
                self.store.update_intent_status(
                    order.idempotency_key, ExecutionStatus.REJECTED.value
                )
                return self.store.get_order(order.local_order_id)
            if order.status is ExecutionStatus.EXPIRED:
                self._transition(
                    order, ExecutionStatus.EXPIRED, "BROKER_EXPIRED",
                    {}, now=now,
                )
                self.store.update_intent_status(
                    order.idempotency_key, ExecutionStatus.EXPIRED.value
                )
                return self.store.get_order(order.local_order_id)
            if order.status is ExecutionStatus.CANCELLED:
                self._handle_cancelled(order, market_price=market_price, now=now)
                return self.store.get_order(order.local_order_id)
            if previous is not order.status:
                self._transition(
                    order,
                    order.status,
                    "BROKER_ORDER_UPDATE",
                    {"broker_status": broker_order.status.value},
                    now=now,
                )
            else:
                self.store.save_order(order)

            if order.status is ExecutionStatus.CANCEL_PENDING:
                return self.store.get_order(order.local_order_id)
            self._maybe_timeout_or_chase(
                order, market_price=market_price, now=now
            )
            return self.store.get_order(order.local_order_id)

        return self._handle_missing_broker_order(
            order, market_price=market_price, now=now
        )

    def process_all(self, *, prices=None, now=None) -> list[ManagedOrder]:
        """查询所有非终态订单。"""
        prices = prices or {}
        now = now or self.clock()
        result = []
        for order in self.store.list_active_orders():
            result.append(self.process_order(
                order.local_order_id,
                market_price=prices.get(order.symbol),
                now=now,
            ))
        return result

    def cancel_order(self, local_order_id: str,
                     reason: str = "manual cancel") -> ManagedOrder:
        """请求撤销剩余订单，不重复发送撤单。"""
        order = self.store.get_order(local_order_id)
        if order.is_terminal:
            return order
        try:
            self.session_guard.require_cancel(now=self.clock())
        except TradingSessionClosed as exc:
            order.last_error = str(exc)
            self.store.save_order(order)
            self._record_event(
                order,
                "CANCEL_BLOCKED_BY_SESSION",
                details={"reason": str(exc)},
            )
            return order
        if order.broker_order_id is None:
            return self._transition(
                order, ExecutionStatus.CANCELLED, "LOCAL_PRE_SUBMIT_CANCEL",
                {"reason": reason},
            )
        if order.status is ExecutionStatus.CANCEL_PENDING:
            return order
        if not self.broker.cancel_order(order.broker_order_id):
            return order
        order.cancel_requested_at = self.clock()
        return self._transition(
            order, ExecutionStatus.CANCEL_PENDING, "CANCEL_REQUESTED",
            {"reason": reason},
        )

    def recover(self, *, now=None) -> list[ManagedOrder]:
        """程序启动后恢复非终态订单。"""
        if not getattr(self.broker, "_connected", False):
            self.broker.connect()
        now = now or self.clock()
        try:
            self.session_guard.require_open(
                action="recover", now=now
            )
        except TradingSessionClosed as exc:
            orders = self.store.list_active_orders()
            for order in orders:
                order.last_error = str(exc)
                self.store.save_order(order)
                self._record_event(
                    order,
                    "RECOVERY_BLOCKED_BY_SESSION",
                    details={"reason": str(exc)},
                )
            return orders
        recovered = []
        for order in self.store.list_active_orders():
            recovered.append(self.process_order(
                order.local_order_id,
                market_price=self._market_price(order.symbol),
                now=now,
            ))
        return recovered

    def get_order(self, local_order_id: str) -> ManagedOrder:
        return self.store.get_order(local_order_id)

    def get_orders_for_intent(self, intent_id: str) -> list[ManagedOrder]:
        return self.store.orders_for_intent(intent_id)

    def list_orders(self) -> list[ManagedOrder]:
        return self.store.list_orders()

    def get_events(self, local_order_id: str) -> list[ExecutionEvent]:
        return self.store.list_events(local_order_id)

    def get_fills(self, local_order_id: str):
        return self.store.list_fills(local_order_id)

    def _submit(self, order: ManagedOrder, *,
                market_price: float | None = None):
        if order.status not in {
            ExecutionStatus.PENDING_SUBMIT,
            ExecutionStatus.CREATED,
        }:
            return
        try:
            self.session_guard.require_order(
                order.side, now=self.clock()
            )
        except TradingSessionClosed as exc:
            order.last_error = str(exc)
            self.store.save_order(order)
            self._record_event(
                order,
                "SUBMIT_BLOCKED_BY_SESSION",
                details={"reason": str(exc)},
            )
            return
        self._transition(
            order, ExecutionStatus.SUBMITTING, "SUBMIT_STARTED"
        )
        request = OrderRequest(
            symbol=order.symbol,
            side=order.side,
            price=order.limit_price,
            quantity=order.remaining_quantity,
            client_order_id=order.client_order_id,
            strategy_name=str(order.metadata.get("strategy_name", "quant")),
            remark=str(order.metadata.get("remark", "")),
        )
        try:
            broker_order_id = self.broker.submit_order(request)
        except BrokerOrderUnknownError as exc:
            order.last_error = str(exc)
            self._transition(
                order, ExecutionStatus.UNKNOWN, "SUBMIT_RESULT_UNKNOWN",
                {"error": str(exc)},
            )
            return
        except BrokerOrderError as exc:
            order.last_error = str(exc)
            if self.policy.retry_on_rejected and \
                    self._can_create_attempt(order):
                self._transition(
                    order, ExecutionStatus.REJECTED,
                    "BROKER_REJECTED_RETRY",
                    {"error": str(exc)},
                )
                self._retry_remaining(order, market_price)
            else:
                self._transition(
                    order, ExecutionStatus.REJECTED, "BROKER_REJECTED",
                    {"error": str(exc)},
                )
                self.store.update_intent_status(
                    order.idempotency_key, ExecutionStatus.REJECTED.value
                )
            return
        except (BrokerConnectionError, TimeoutError, OSError) as exc:
            order.last_error = str(exc)
            self._transition(
                order, ExecutionStatus.UNKNOWN, "SUBMIT_RESULT_UNKNOWN",
                {"error": str(exc)},
            )
            return
        order.broker_order_id = str(broker_order_id)
        order.submitted_at = self.clock()
        self._transition(
            order, ExecutionStatus.SUBMITTED, "BROKER_ACCEPTED",
            {"broker_order_id": order.broker_order_id},
        )
        try:
            self.process_order(order.local_order_id, market_price=market_price)
        except Exception:
            # 提交成功但立即查询失败时，保持 SUBMITTED，下一次轮询恢复。
            pass

    def _handle_missing_broker_order(
            self, order: ManagedOrder, *, market_price, now):
        if order.status in {
            ExecutionStatus.PENDING_SUBMIT,
            ExecutionStatus.CREATED,
        }:
            self._submit(order, market_price=market_price)
            return self.store.get_order(order.local_order_id)
        if order.status in {
            ExecutionStatus.SUBMITTING,
            ExecutionStatus.UNKNOWN,
        }:
            base = order.submitted_at or order.last_event_at or now
            if (now - base).total_seconds() < self.policy.unknown_grace_seconds:
                return self.store.get_order(order.local_order_id)
            self._transition(
                order, ExecutionStatus.RECONCILING,
                "SUBMIT_NOT_FOUND_AFTER_GRACE",
            )
            if self.policy.retry_on_unknown and \
                    self._can_create_attempt(order):
                self._retry_remaining(order, market_price)
            else:
                self._transition(
                    order, ExecutionStatus.FAILED, "RETRY_NOT_ALLOWED"
                )
            return self.store.get_order(order.local_order_id)
        if order.status in {
            ExecutionStatus.SUBMITTED,
            ExecutionStatus.PARTIALLY_FILLED,
            ExecutionStatus.CANCEL_PENDING,
        }:
            self._transition(
                order, ExecutionStatus.UNKNOWN, "ORDER_QUERY_MISSING"
            )
        return self.store.get_order(order.local_order_id)

    def _handle_cancelled(self, order: ManagedOrder, *,
                          market_price, now):
        target = order.metadata.get("chase_target")
        if target is not None and order.remaining_quantity > 0 and \
                self._can_create_attempt(order):
            self._transition(
                order, ExecutionStatus.CANCELLED, "CHASE_CANCEL_CONFIRMED",
                {"chase_target": target},
                now=now,
            )
            self._retry_remaining(order, market_price, limit_price=target)
            return
        self._transition(
            order, ExecutionStatus.CANCELLED, "CANCEL_CONFIRMED", now=now
        )
        total_filled = self._total_filled(order.intent_id)
        intent_status = (
            ExecutionStatus.FILLED
            if total_filled >= order.metadata.get(
                "intent_quantity", order.requested_quantity
            )
            else ExecutionStatus.CANCELLED
        )
        self.store.update_intent_status(
            order.idempotency_key, intent_status.value
        )

    def _maybe_timeout_or_chase(self, order: ManagedOrder, *,
                                market_price, now):
        base = order.submitted_at or order.last_event_at or now
        elapsed = (now - base).total_seconds()
        if elapsed >= self.policy.cancel_after_seconds:
            self.cancel_order(order.local_order_id, "order timeout")
            return
        if elapsed < self.policy.chase_after_seconds:
            return
        if market_price is not None and \
                order.chase_steps < self.policy.max_chase_steps:
            target = self._chase_price(order, market_price)
            if target is not None and abs(target - order.limit_price) >= 1e-9:
                order.metadata["chase_target"] = target
                order.chase_steps += 1
                self.store.save_order(order)
                self.cancel_order(
                    order.local_order_id,
                    f"limited chase to {target:.4f}",
                )
                return

    def _chase_price(self, order: ManagedOrder,
                     market_price: float) -> float | None:
        step = self.policy.price_tick * self.policy.chase_step_ticks
        if order.side is OrderSide.BUY:
            target = max(order.limit_price, float(market_price))
            target = min(target, order.limit_price + step)
            hard = order.hard_limit_price
            if hard is None:
                hard = order.metadata.get("initial_limit_price", order.limit_price)
                hard = hard * (1 + self.policy.max_price_deviation_pct)
            target = min(target, float(hard))
        else:
            target = min(order.limit_price, float(market_price))
            target = max(target, order.limit_price - step)
            hard = order.hard_limit_price
            if hard is None:
                hard = order.metadata.get("initial_limit_price", order.limit_price)
                hard = hard * (1 - self.policy.max_price_deviation_pct)
            target = max(target, float(hard))
        return round(target, 2)

    def _retry_remaining(self, order: ManagedOrder, market_price,
                         limit_price: float | None = None):
        remaining = self._intent_remaining(order)
        if remaining <= 0:
            return
        orders = self.store.orders_for_intent(order.intent_id)
        attempt_no = max(item.attempt_no for item in orders) + 1
        if attempt_no > self.policy.max_attempts:
            return
        new_order = self._new_order(
            self.store.get_intent(order.idempotency_key),
            attempt_no=attempt_no,
            quantity=remaining,
            limit_price=limit_price or order.limit_price,
            chase_steps=order.chase_steps,
        )
        self.store.create_order(new_order)
        self._record_event(new_order, "RETRY_ATTEMPT_CREATED", details={
            "previous_order_id": order.local_order_id,
        })
        self._submit(new_order, market_price=market_price)

    def _new_order(self, intent, *, attempt_no, quantity,
                   limit_price, chase_steps) -> ManagedOrder:
        client_order_id = _client_order_id(
            intent.idempotency_key, attempt_no
        )
        return ManagedOrder(
            local_order_id=str(uuid.uuid4()),
            intent_id=intent.intent_id,
            idempotency_key=intent.idempotency_key,
            attempt_no=attempt_no,
            client_order_id=client_order_id,
            symbol=intent.symbol,
            side=intent.side,
            requested_quantity=int(quantity),
            limit_price=float(limit_price),
            hard_limit_price=intent.hard_limit_price,
            status=ExecutionStatus.PENDING_SUBMIT,
            chase_steps=int(chase_steps),
            metadata={
                "strategy_name": intent.metadata.get("strategy_name", "quant"),
                "remark": intent.metadata.get("remark", ""),
                "initial_limit_price": intent.limit_price,
                "intent_quantity": intent.quantity,
            },
        )

    def _find_broker_order(self, order: ManagedOrder):
        if order.broker_order_id:
            try:
                return self.broker.get_order(order.broker_order_id)
            except BrokerError:
                pass
        try:
            candidates = self.broker.get_orders(symbol=order.symbol)
        except BrokerError:
            return None
        for candidate in candidates:
            if candidate.client_order_id == order.client_order_id:
                return candidate
        return None

    def _ingest_trades(self, order: ManagedOrder):
        if not order.broker_order_id:
            return
        try:
            trades = self.broker.get_trades(order_id=order.broker_order_id)
        except BrokerError:
            return
        for trade in trades:
            if self.store.save_fill(order.local_order_id, trade):
                self._record_event(
                    order, "FILL_RECORDED",
                    details={"trade_id": trade.trade_id},
                )

    def _transition(self, order: ManagedOrder, status: ExecutionStatus,
                    event_type: str, details=None, now=None):
        previous = order.status
        order.status = status
        order.last_event_at = now or self.clock()
        if status.terminal:
            order.terminal_at = order.last_event_at
        self.store.save_order(order)
        self.store.append_event(ExecutionEvent(
            event_id=str(uuid.uuid4()),
            local_order_id=order.local_order_id,
            event_type=event_type,
            from_status=previous.value,
            to_status=status.value,
            created_at=order.last_event_at,
            details=dict(details or {}),
        ))
        return order

    def _record_event(self, order, event_type, details=None):
        self.store.append_event(ExecutionEvent(
            event_id=str(uuid.uuid4()),
            local_order_id=order.local_order_id,
            event_type=event_type,
            from_status=order.status.value,
            to_status=order.status.value,
            created_at=self.clock(),
            details=dict(details or {}),
        ))

    def _can_create_attempt(self, order: ManagedOrder) -> bool:
        orders = self.store.orders_for_intent(order.intent_id)
        return len(orders) < self.policy.max_attempts

    def _total_filled(self, intent_id: str) -> int:
        return sum(
            item.filled_quantity
            for item in self.store.orders_for_intent(intent_id)
        )

    def _intent_remaining(self, order: ManagedOrder) -> int:
        intent = self.store.get_intent(order.idempotency_key)
        return max(0, intent.quantity - self._total_filled(order.intent_id))

    def _market_price(self, symbol):
        return self.price_provider(symbol) if self.price_provider else None


def _client_order_id(idempotency_key: str, attempt_no: int) -> str:
    return f"{idempotency_key}:{int(attempt_no)}"
