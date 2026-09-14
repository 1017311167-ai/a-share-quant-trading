"""模拟盘持续运行、回放和故障注入。"""

from trading.models import ReplayEvent, SignalAction, TradingSignal
from trading.runtime import PaperTradingRuntime
from trading.safety import PaperTradingSafetyError, assert_paper_trading
from trading.verification import LinkVerificationReport, verify_paper_link


__all__ = [
    "PaperTradingRuntime",
    "PaperTradingSafetyError",
    "ReplayEvent",
    "SignalAction",
    "TradingSignal",
    "LinkVerificationReport",
    "assert_paper_trading",
    "verify_paper_link",
]
