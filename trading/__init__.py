"""模拟盘持续运行、回放和故障注入。"""

from trading.models import ReplayEvent, SignalAction, TradingSignal
from trading.safety import PaperTradingSafetyError, assert_paper_trading
from trading.session import (
    TradingSessionClosed,
    TradingSessionDecision,
    TradingSessionGuard,
)
from trading.verification import LinkVerificationReport, verify_paper_link


__all__ = [
    "PaperTradingRuntime",
    "PaperTradingSafetyError",
    "ReplayEvent",
    "SignalAction",
    "TradingSignal",
    "TradingSessionClosed",
    "TradingSessionDecision",
    "TradingSessionGuard",
    "LinkVerificationReport",
    "assert_paper_trading",
    "verify_paper_link",
]


def __getattr__(name):
    if name == "PaperTradingRuntime":
        from trading.runtime import PaperTradingRuntime

        return PaperTradingRuntime
    raise AttributeError(name)
