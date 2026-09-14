"""QMT 模拟账户持续运行宿主。"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
import uuid

from broker_adapter.base_broker import (
    BrokerConnectionError,
    BrokerDataError,
)
from broker_adapter.models import normalize_symbol
from execution.models import ExecutionResult, OrderIntent
from persistence.reconciliation import ReconciliationService
from persistence.recovery import RecoveryService
from ops.logging_setup import log_event
from ops.metrics import METRICS
from risk.engine import execute_kill_switch, risk_context_from_broker
from risk.models import QuoteState
from trading.models import SignalAction, TradingSignal
from trading.safety import assert_paper_trading
from trading.session import TradingSessionClosed


class PaperTradingRuntime:
    """把信号、风控、执行、券商回调、持久化和对账串成持续运行循环。"""

    def __init__(
            self,
            *,
            repository,
            broker,
            account_id: str,
            risk_engine,
            execution_manager,
            execution_bridge,
            reconciliation_service: ReconciliationService,
            recovery_service: RecoveryService,
            signal_feed=None,
            poll_interval: float = 1.0,
            reconcile_interval: float = 300.0,
            clock=None,
            sleeper=None,
            metrics=None,
            alert_manager=None,
    ):
        if not account_id:
            raise ValueError("模拟盘运行必须提供 account_id")
        if risk_engine is None:
            raise ValueError("模拟盘运行必须提供 RiskEngine")
        self.repository = repository
        self.broker = broker
        self.account_id = account_id
        self.risk_engine = risk_engine
        self.execution_manager = execution_manager
        self.execution_bridge = execution_bridge
        self.reconciliation_service = reconciliation_service
        self.recovery_service = recovery_service
        self.signal_feed = signal_feed
        self.poll_interval = max(0.05, float(poll_interval))
        self.reconcile_interval = max(
            self.poll_interval, float(reconcile_interval)
        )
        self.clock = clock or dt.datetime.now
        self.sleeper = sleeper or time.sleep
        self.metrics = metrics or METRICS
        self.alert_manager = alert_manager
        self.logger = logging.getLogger("trading.runtime")
        self.quotes: dict[str, QuoteState] = {}
        self.state = "created"
        self.recovery_case_id = None
        self.last_reconcile_at = None
        self.cycle_count = 0
        self.runtime_id = str(uuid.uuid4())
        self.last_error = ""
        self._stop_event = threading.Event()
        self.execution_manager.require_risk = True
        self.execution_manager.risk_engine = self.risk_engine
        self.execution_manager.risk_context_provider = self.risk_context
        self.metrics.gauge(
            "trading_runtime_online",
            1,
            account_id=self.account_id,
        )

    def start_recovery(self, reason: str = "paper_runtime_startup") -> dict:
        safety = assert_paper_trading(self.broker)
        case = self.recovery_service.start(self.account_id, reason=reason)
        self.recovery_case_id = case["recovery_case_id"]
        self.state = case["status"]
        self.repository.append_audit_event(
            audit_event_id=str(uuid.uuid4()),
            account_id=self.account_id,
            aggregate_type="paper_runtime",
            aggregate_id=self.account_id,
            event_type="runtime_started",
            payload={"reason": reason, "safety": safety, "case": case},
        )
        self._heartbeat()
        log_event(
            self.logger,
            "recovery_started",
            "模拟盘恢复检查完成",
            account_id=self.account_id,
            state=self.state,
            case_id=self.recovery_case_id,
        )
        if self.state != "ready_for_confirmation":
            self._alert(
                "recovery_blocked",
                "模拟盘恢复检查未通过，交易保持冻结",
                level="critical",
                context={
                    "state": self.state,
                    "case_id": self.recovery_case_id,
                },
            )
        return case

    def confirm_recovery(
            self,
            *,
            confirmed_by: str,
            note: str,
    ) -> dict:
        if self.recovery_case_id is None:
            raise ValueError("必须先执行 start_recovery")
        case = self.recovery_service.confirm(
            self.recovery_case_id,
            confirmed_by=confirmed_by,
            note=note,
        )
        self.state = case["status"]
        self._heartbeat()
        log_event(
            self.logger,
            "recovery_confirmed",
            "人工确认恢复完成",
            account_id=self.account_id,
            state=self.state,
            case_id=self.recovery_case_id,
        )
        return case

    def bootstrap_account(
            self,
            *,
            operator: str,
            note: str = "QMT 模拟盘首次建立账户基线",
    ) -> dict:
        assert_paper_trading(self.broker)
        if not getattr(self.broker, "_connected", False):
            self.broker.connect()
        return self.reconciliation_service.bootstrap_account(
            self.account_id,
            resolved_by=operator,
            note=note,
        )

    def on_quote(self, symbol: str, quote) -> QuoteState:
        assert_paper_trading(self.broker)
        normalized = _quote_from_tick(symbol, quote, self.clock())
        self.quotes[normalized.symbol] = normalized
        return normalized

    def risk_context(self):
        return risk_context_from_broker(
            self.broker,
            self.quotes,
            now=self.clock(),
        )

    def handle_signal(self, signal: TradingSignal):
        assert_paper_trading(self.broker)
        if self.state != "confirmed":
            raise RuntimeError(
                f"运行状态为 {self.state}，人工确认恢复前禁止提交信号"
            )
        signal_payload = signal.to_dict()
        saved = self.repository.save_signal(
            signal_id=signal.signal_id,
            strategy_instance_id=signal.strategy_instance_id,
            symbol=signal.symbol,
            action=signal.action.value,
            signal_time=signal.signal_time,
            idempotency_key=signal.idempotency_key,
            status="accepted",
            payload=signal_payload,
        )
        if saved["status"] == "converted":
            local_order_id = (
                saved.get("payload", {}).get("local_order_id")
            )
            existing_order = (
                self.repository.get_order(local_order_id)
                if local_order_id else None
            )
            return ExecutionResult(
                order=(
                    self.execution_manager.get_order(local_order_id)
                    if existing_order else None
                ),
                reused_existing=True,
                message="信号已处理，返回已有订单",
            )
        if saved["status"] in {"rejected", "noop"}:
            return ExecutionResult(
                order=None,
                reused_existing=True,
                message=f"信号已处理，状态为 {saved['status']}",
            )
        if signal.action is SignalAction.NOOP:
            self.metrics.counter(
                "trading_signals_total",
                action="noop",
                result="noop",
            )
            self.repository.update_signal_status(
                saved["signal_id"], "noop", {"handled": True}
            )
            return None
        intent = OrderIntent(
            idempotency_key=f"paper-signal:{signal.idempotency_key}",
            symbol=signal.symbol,
            side=signal.action.value,
            quantity=signal.quantity,
            limit_price=signal.limit_price,
            hard_limit_price=signal.limit_price,
            strategy_id=signal.strategy_instance_id,
            metadata={
                "strategy_name": signal.strategy_instance_id,
                "signal_id": signal.signal_id,
                "signal_idempotency_key": signal.idempotency_key,
                "data_version": signal.data_version,
                **dict(signal.metadata),
            },
        )
        try:
            result = self.execution_manager.submit_intent(
                intent,
                risk_context=self.risk_context(),
            )
        except TradingSessionClosed as exc:
            self.repository.update_signal_status(
                saved["signal_id"],
                "rejected",
                {
                    "rule_codes": ["TRADING_SESSION_CLOSED"],
                    "message": str(exc),
                    "session": exc.decision.session,
                },
            )
            log_event(
                self.logger,
                "signal_blocked_by_session",
                "信号被交易时段拦截",
                level=logging.WARNING,
                account_id=self.account_id,
                symbol=signal.symbol,
                action=signal.action.value,
                reason=str(exc),
            )
            return ExecutionResult(order=None, message=str(exc))
        if result.risk_decision is not None and not result.risk_decision.approved:
            self.repository.update_signal_status(
                saved["signal_id"],
                "rejected",
                {
                    "risk_decision": {
                        "rule_codes": list(
                            result.risk_decision.rule_codes
                        ),
                        "reasons": list(result.risk_decision.reasons),
                        "trading_state": (
                            result.risk_decision.trading_state.value
                        ),
                    },
                    "message": result.message,
                },
            )
            self.execution_bridge.sync_manager(self.execution_manager)
            self.metrics.counter(
                "trading_signals_total",
                action=signal.action.value,
                result="rejected",
            )
            log_event(
                self.logger,
                "signal_rejected",
                "信号未通过风控",
                level=logging.WARNING,
                account_id=self.account_id,
                symbol=signal.symbol,
                rules=list(result.risk_decision.rule_codes),
            )
            return result
        self.repository.update_signal_status(
            saved["signal_id"],
            "converted",
            {
                "order_intent_id": intent.intent_id,
                "local_order_id": (
                    result.order.local_order_id if result.order else None
                ),
                "reused_existing": result.reused_existing,
            },
        )
        self.execution_bridge.sync_manager(self.execution_manager)
        self.metrics.counter(
            "trading_signals_total",
            action=signal.action.value,
            result="converted",
        )
        if result.order is not None:
            self.metrics.counter(
                "trading_orders_total",
                side=result.order.side.value,
                status=result.order.status.value,
            )
        log_event(
            self.logger,
            "signal_executed",
            "策略信号已进入执行链",
            account_id=self.account_id,
            symbol=signal.symbol,
            action=signal.action.value,
            local_order_id=(
                result.order.local_order_id if result.order else None
            ),
        )
        self.repository.append_audit_event(
            audit_event_id=str(uuid.uuid4()),
            account_id=self.account_id,
            aggregate_type="signal",
            aggregate_id=signal.signal_id,
            event_type="signal_executed",
            payload={
                "intent_id": intent.intent_id,
                "local_order_id": (
                    result.order.local_order_id if result.order else None
                ),
                "submitted": result.submitted,
            },
        )
        return result

    def cancel_active_order(self, local_order_id: str | None = None):
        assert_paper_trading(self.broker)
        if local_order_id is None:
            active = self.repository.list_orders(
                account_id=self.account_id, active_only=True
            )
            if not active:
                return None
            local_order_id = active[-1]["local_order_id"]
        order = self.execution_manager.cancel_order(local_order_id)
        if not order.is_terminal:
            order = self.execution_manager.process_order(local_order_id)
        self.execution_bridge.sync_manager(self.execution_manager)
        return order

    def process_once(self) -> dict:
        self.cycle_count += 1
        self.metrics.counter(
            "trading_runtime_cycles_total",
            account_id=self.account_id,
        )
        assert_paper_trading(self.broker)
        commands = self._process_runtime_commands()
        if self.state != "confirmed":
            self._heartbeat()
            return {
                "cycle": self.cycle_count,
                "skipped": True,
                "state": self.state,
                "commands": commands,
            }
        self.risk_engine.check_runtime(self.risk_context())
        prices = {
            symbol: item.last_price for symbol, item in self.quotes.items()
        }
        orders = self.execution_manager.process_all(
            prices=prices, now=self.clock()
        )
        sync = self.execution_bridge.sync_manager(self.execution_manager)
        reconciliation = None
        now = self.clock()
        if (
            self._reconciliation_due(now)
        ):
            reconciliation = self.reconciliation_service.reconcile(
                self.account_id
            )
            self.last_reconcile_at = now
            if reconciliation.status != "matched":
                self.risk_engine.enable_stop_open(
                    "持续运行对账发现差异，等待人工处理"
                )
                self.state = "blocked"
                self._alert(
                    "reconciliation_mismatch",
                    "持续运行对账发现差异，已停止开仓",
                    level="critical",
                    context={
                        "run_id": reconciliation.run_id,
                        "difference_count": reconciliation.difference_count,
                    },
                )
            self.metrics.gauge(
                "trading_reconciliation_differences",
                reconciliation.difference_count,
                account_id=self.account_id,
            )
        self._heartbeat()
        return {
            "cycle": self.cycle_count,
            "orders": [item.to_dict() for item in orders],
            "sync": sync,
            "reconciliation": (
                reconciliation.__dict__ if reconciliation else None
            ),
            "commands": commands,
        }

    def handle_connection_lost(self, message: str = ""):
        self.risk_engine.on_connection_status(
            False, message or "QMT 模拟账户连接断开"
        )
        self.state = "blocked"
        self.last_error = message
        self.metrics.gauge(
            "trading_runtime_online",
            0,
            account_id=self.account_id,
        )
        self._alert(
            "broker_disconnected",
            "QMT 模拟账户连接断开，交易已冻结",
            level="critical",
            context={"message": message},
        )
        self.repository.append_audit_event(
            audit_event_id=str(uuid.uuid4()),
            account_id=self.account_id,
            aggregate_type="broker_connection",
            aggregate_id=self.account_id,
            event_type="connection_lost",
            payload={"message": message},
        )
        self._heartbeat()

    def reconnect_and_reconcile(self, max_attempts: int = 3):
        self.broker.reconnect(max_attempts=max_attempts, delay=0)
        self.risk_engine.on_connection_status(
            True, "QMT 模拟账户连接已恢复"
        )
        case = self.recovery_service.start(
            self.account_id,
            reason="broker_connection_recovered",
        )
        self.recovery_case_id = case["recovery_case_id"]
        self.state = case["status"]
        self._heartbeat()
        return case

    def run_forever(
            self,
            *,
            stop_event=None,
            max_cycles: int | None = None,
    ) -> int:
        if self.state != "confirmed":
            raise RuntimeError("人工确认恢复前禁止启动持续运行循环")
        stop_event = stop_event or self._stop_event
        cycles = 0
        while not stop_event.is_set():
            try:
                if self.signal_feed is not None:
                    for signal in self.signal_feed.poll():
                        self.handle_signal(signal)
                self.process_once()
            except (BrokerConnectionError, BrokerDataError, OSError) as exc:
                self.handle_connection_lost(str(exc))
                try:
                    self.reconnect_and_reconcile()
                except Exception as reconnect_error:
                    self.last_error = (
                        f"{type(reconnect_error).__name__}: {reconnect_error}"
                    )
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            if not stop_event.is_set():
                self.sleeper(self.poll_interval)
        return cycles

    def stop(self):
        self._stop_event.set()

    def _reconciliation_due(self, now):
        if self.last_reconcile_at is None:
            return True
        return (
            now - self.last_reconcile_at
        ).total_seconds() >= self.reconcile_interval

    def _process_runtime_commands(self):
        commands = self.repository.claim_runtime_commands(
            account_id=self.account_id,
            worker_id=self.runtime_id,
            limit=10,
            claimed_at=self.clock(),
        )
        results = []
        for command in commands:
            try:
                result = self._execute_runtime_command(command)
                completed = self.repository.complete_runtime_command(
                    command["command_id"],
                    status="completed",
                    result=result,
                    completed_at=self.clock(),
                )
            except Exception as exc:
                completed = self.repository.complete_runtime_command(
                    command["command_id"],
                    status="failed",
                    result={
                        "error": f"{type(exc).__name__}: {exc}"
                    },
                    completed_at=self.clock(),
                )
            results.append(completed)
            self.metrics.counter(
                "trading_command_total",
                command=command["command_type"],
                status=completed["status"],
            )
            log_event(
                self.logger,
                "runtime_command",
                "运行命令已处理",
                account_id=self.account_id,
                command=command["command_type"],
                command_status=completed["status"],
                requested_by=command["requested_by"],
            )
        return results

    def _execute_runtime_command(self, command):
        command_type = command["command_type"]
        payload = command.get("payload") or {}
        reason = str(
            payload.get("reason")
            or f"交易台命令 {command_type}，操作人 {command['requested_by']}"
        )
        if command_type == "stop_open":
            self.risk_engine.enable_stop_open(reason)
            return {"risk_state": self.risk_engine.state.value, "reason": reason}
        if command_type == "reduce_only":
            self.risk_engine.enable_reduce_only(reason)
            return {"risk_state": self.risk_engine.state.value, "reason": reason}
        if command_type == "kill_switch":
            cancelled = execute_kill_switch(
                self.broker,
                self.risk_engine,
                reason,
                session_guard=self.execution_manager.session_guard,
            )
            self.execution_bridge.sync_manager(self.execution_manager)
            self.state = "blocked"
            self._alert(
                "kill_switch",
                "全局急停已执行并发出全部撤单",
                level="critical",
                context={"cancelled_order_count": cancelled},
            )
            return {
                "risk_state": self.risk_engine.state.value,
                "cancelled_order_count": cancelled,
                "reason": reason,
            }
        if command_type == "reconcile":
            result = self.reconciliation_service.reconcile(self.account_id)
            if result.status != "matched":
                self.risk_engine.enable_stop_open(
                    "手工对账发现差异，等待处理"
                )
                self.state = "blocked"
            return {
                "reconciliation_status": result.status,
                "difference_count": result.difference_count,
                "run_id": result.run_id,
            }
        raise ValueError(f"不支持的运行命令：{command_type!r}")

    def _heartbeat(self):
        self.repository.save_runtime_heartbeat(
            account_id=self.account_id,
            runtime_id=self.runtime_id,
            status=self.state,
            environment="paper",
            last_cycle_at=self.clock(),
            payload={
                "cycle_count": self.cycle_count,
                "last_error": self.last_error,
                "risk_state": self.risk_engine.state.value,
            },
        )
        self.metrics.gauge(
            "trading_runtime_online",
            1,
            account_id=self.account_id,
        )
        self.metrics.gauge(
            "trading_risk_state",
            _risk_state_number(self.risk_engine.state.value),
            account_id=self.account_id,
        )

    def _alert(self, rule, message, *, level, context):
        if self.alert_manager is None:
            return
        try:
            self.alert_manager.send(
                rule,
                message,
                level=level,
                context={"account_id": self.account_id, **context},
            )
        except Exception as exc:
            log_event(
                self.logger,
                "alert_failed",
                "告警发送失败，交易流程继续",
                level=logging.ERROR,
                rule=rule,
                error=f"{type(exc).__name__}: {exc}",
            )


def _quote_from_tick(symbol, tick, now):
    if isinstance(tick, QuoteState):
        return tick
    values = tick if isinstance(tick, dict) else {}
    last_price = _first_number(
        values,
        "lastPrice", "last_price", "last", "price",
    )
    if last_price is None or last_price <= 0:
        raise ValueError(f"行情 {symbol} 缺少有效最新价")
    previous_close = _first_number(
        values,
        "lastClose", "preClose", "previous_close", "prev_close",
    )
    limit_up = _first_number(values, "limitUp", "limit_up", "upperLimit")
    limit_down = _first_number(
        values, "limitDown", "limit_down", "lowerLimit"
    )
    timestamp = _quote_time(values, now)
    tradable = not bool(
        values.get("suspended")
        or values.get("isSuspended")
        or values.get("tradable") is False
    )
    return QuoteState(
        symbol=normalize_symbol(symbol),
        last_price=float(last_price),
        previous_close=previous_close,
        limit_up=limit_up,
        limit_down=limit_down,
        timestamp=timestamp,
        tradable=tradable,
    )


def _first_number(values, *keys):
    for key in keys:
        value = values.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _quote_time(values, fallback):
    value = values.get("timestamp") or values.get("time")
    if value is None:
        return fallback
    if isinstance(value, dt.datetime):
        return value
    try:
        return dt.datetime.fromtimestamp(float(value))
    except (TypeError, ValueError, OSError):
        return fallback


def _risk_state_number(state):
    return {
        "active": 0,
        "stop_open": 1,
        "reduce_only": 2,
        "killed": 3,
    }.get(str(state), -1)
