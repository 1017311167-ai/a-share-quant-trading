"""事件驱动行情回放和故障演练。"""

from __future__ import annotations

from dataclasses import dataclass, field

from trading.models import ReplayEvent, TradingSignal


@dataclass
class ReplayResult:
    events: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class ReplayRunner:
    """按事件时间顺序驱动 PaperTradingRuntime，不依赖真实券商。"""

    def __init__(self, runtime):
        self.runtime = runtime

    def run(self, events) -> ReplayResult:
        result = ReplayResult()
        for event in sorted(events, key=lambda item: item.at):
            try:
                output = self._dispatch(event)
                result.events.append({
                    "event_type": event.event_type,
                    "at": event.at,
                    "output": _serialize(output),
                })
            except Exception as exc:
                result.errors.append(
                    f"{event.at.isoformat()} {event.event_type}: "
                    f"{type(exc).__name__}: {exc}"
                )
        return result

    def _dispatch(self, event: ReplayEvent):
        payload = dict(event.payload)
        if event.event_type == "quote":
            return self.runtime.on_quote(
                payload["symbol"], payload.get("quote", payload)
            )
        if event.event_type == "signal":
            signal = payload.get("signal")
            if signal is None:
                signal = TradingSignal.from_dict(payload)
            return self.runtime.handle_signal(signal)
        if event.event_type == "cancel":
            local_order_id = payload.get("local_order_id")
            if local_order_id is None and payload.get("broker_order_id"):
                local_order_id = _local_order_id_for_broker(
                    self.runtime, payload["broker_order_id"]
                )
            return self.runtime.cancel_active_order(local_order_id)
        if event.event_type == "poll":
            return self.runtime.process_once()
        if event.event_type == "reconcile":
            return self.runtime.reconciliation_service.reconcile(
                self.runtime.account_id
            )
        if event.event_type == "disconnect":
            message = str(payload.get("message", "replay disconnect"))
            if hasattr(self.runtime.broker, "simulate_disconnect"):
                self.runtime.broker.simulate_disconnect()
            else:
                self.runtime.broker.disconnect()
            self.runtime.handle_connection_lost(message)
            return {"connected": False}
        if event.event_type == "reconnect":
            return self.runtime.reconnect_and_reconcile(
                int(payload.get("max_attempts", 3))
            )
        if event.event_type == "settle":
            if not hasattr(self.runtime.broker, "settle_positions"):
                raise RuntimeError("当前 Broker 不支持日终结算")
            self.runtime.broker.settle_positions()
            return {"settled": True}
        raise ValueError(f"不支持的回放事件：{event.event_type}")


def _local_order_id_for_broker(runtime, broker_order_id):
    order = runtime.repository.find_order_by_broker_id(broker_order_id)
    if order is None:
        raise KeyError(f"未找到券商订单对应本地订单：{broker_order_id}")
    return order["local_order_id"]


def _serialize(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dict__"):
        return {
            key: _serialize(item) for key, item in value.__dict__.items()
        }
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    return str(value)
