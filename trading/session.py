"""A 股交易时段统一闸门。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from broker_adapter.models import OrderSide
from data.trading_calendar import TradingCalendar, load_trading_calendar


SHANGHAI = ZoneInfo("Asia/Shanghai")
MORNING_OPEN = dt.time(9, 30)
MORNING_CLOSE = dt.time(11, 30)
AFTERNOON_OPEN = dt.time(13, 0)
AFTERNOON_CLOSE = dt.time(15, 0)


@dataclass(frozen=True)
class TradingSessionDecision:
    allowed: bool
    reason: str
    local_time: dt.datetime
    trading_day: bool
    session: str = ""


class TradingSessionClosed(RuntimeError):
    def __init__(self, decision: TradingSessionDecision):
        self.decision = decision
        super().__init__(decision.reason)


class TradingSessionGuard:
    """在券商调用前检查交易日和连续竞价时段。"""

    def __init__(
            self,
            *,
            calendar: TradingCalendar | None = None,
            calendar_provider=None,
            clock=None,
            timezone=SHANGHAI,
            allow_fallback=False,
            enabled=True,
    ):
        self.calendar = calendar
        self.calendar_provider = calendar_provider
        self.clock = clock or dt.datetime.now
        self.timezone = timezone
        self.allow_fallback = bool(allow_fallback)
        self.enabled = bool(enabled)
        self._calendar_cache = {}

    @classmethod
    def always_open(cls):
        return cls(enabled=False)

    def check(self, now=None) -> TradingSessionDecision:
        local_time = self._localize(now or self.clock())
        if not self.enabled:
            return TradingSessionDecision(
                True, "交易时段检查已关闭", local_time, True, "disabled"
            )
        calendar = self._calendar_for(local_time.date())
        trading_day = calendar.is_trading_day(local_time.date())
        if not trading_day:
            return TradingSessionDecision(
                False,
                f"{local_time.date()} 不是交易日",
                local_time,
                False,
            )
        current = local_time.timetz().replace(tzinfo=None)
        if MORNING_OPEN <= current <= MORNING_CLOSE:
            return TradingSessionDecision(
                True, "上午连续竞价", local_time, True, "morning"
            )
        if AFTERNOON_OPEN <= current <= AFTERNOON_CLOSE:
            return TradingSessionDecision(
                True, "下午连续竞价", local_time, True, "afternoon"
            )
        if MORNING_CLOSE < current < AFTERNOON_OPEN:
            reason = "当前处于午间休市，不允许提交或撤销订单"
        else:
            reason = (
                f"当前时间 {current:%H:%M} 不在 A 股连续竞价时段"
                "（09:30-11:30、13:00-15:00）"
            )
        return TradingSessionDecision(
            False, reason, local_time, True
        )

    def require_open(self, *, action="order", now=None, emergency=False):
        decision = self.check(now)
        if decision.allowed:
            return decision
        if emergency and action == "cancel":
            return TradingSessionDecision(
                True,
                f"紧急撤单允许在非交易时段执行：{decision.reason}",
                decision.local_time,
                decision.trading_day,
                "emergency_cancel",
            )
        raise TradingSessionClosed(
            TradingSessionDecision(
                False,
                f"{_action_name(action)}被交易时段拦截：{decision.reason}",
                decision.local_time,
                decision.trading_day,
                decision.session,
            )
        )

    def require_order(self, side: OrderSide, *, now=None):
        return self.require_open(action=f"order:{side.value}", now=now)

    def require_cancel(self, *, now=None, emergency=False):
        return self.require_open(
            action="cancel", now=now, emergency=emergency
        )

    def _calendar_for(self, day):
        if self.calendar is not None:
            return self.calendar
        key = (day.year, day.month)
        if key not in self._calendar_cache:
            if self.calendar_provider is not None:
                calendar = self.calendar_provider(day)
            else:
                start = day - dt.timedelta(days=31)
                end = day + dt.timedelta(days=31)
                calendar = load_trading_calendar(
                    start,
                    end,
                    allow_fallback=self.allow_fallback,
                )
            if not isinstance(calendar, TradingCalendar):
                raise TypeError("calendar_provider 必须返回 TradingCalendar")
            self._calendar_cache[key] = calendar
        return self._calendar_cache[key]

    def _localize(self, value):
        if value.tzinfo is None:
            return value.replace(tzinfo=self.timezone)
        return value.astimezone(self.timezone)


def _action_name(action):
    mapping = {
        "order:buy": "买入委托",
        "order:sell": "卖出委托",
        "cancel": "撤单",
    }
    return mapping.get(action, action)
