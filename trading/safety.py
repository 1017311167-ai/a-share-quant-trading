"""模拟盘安全门禁：当前阶段禁止真实资金连接。"""

from __future__ import annotations

import os


class PaperTradingSafetyError(RuntimeError):
    """非模拟环境或真实交易开关被打开时拒绝运行。"""


def assert_paper_trading(broker):
    stage = os.getenv("TRADING_STAGE", "paper").strip().lower()
    if stage in {"real", "live", "production", "实盘"}:
        raise PaperTradingSafetyError(
            f"当前代码基线禁止真实资金交易：TRADING_STAGE={stage!r}"
        )
    allow_real = os.getenv("QMT_ALLOW_REAL_TRADING", "").strip().lower()
    if allow_real in {"1", "true", "yes", "on"}:
        raise PaperTradingSafetyError(
            "当前阶段禁止 QMT_ALLOW_REAL_TRADING=true"
        )
    if not getattr(broker, "is_simulation", False):
        raise PaperTradingSafetyError(
            f"Broker {type(broker).__name__} 未声明为模拟环境"
        )
    mode = str(getattr(broker, "trading_mode", "SIMULATION")).upper()
    if mode not in {"SIMULATION", "PAPER"}:
        raise PaperTradingSafetyError(
            f"Broker trading_mode={mode!r}，当前只允许 SIMULATION"
        )
    return {
        "stage": "paper",
        "broker": type(broker).__name__,
        "trading_mode": mode,
        "real_trading_allowed": False,
    }
