"""模拟盘完整链路验收报告。"""

from __future__ import annotations

from dataclasses import dataclass, field

from trading.safety import assert_paper_trading


@dataclass
class LinkVerificationReport:
    passed: bool
    checks: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self):
        return {
            "passed": self.passed,
            "checks": self.checks,
            "errors": list(self.errors),
        }


def verify_paper_link(
        repository,
        broker,
        account_id,
        *,
        reconciliation_service=None,
):
    """核对本地记录与券商快照，不修改交易策略。"""
    safety = assert_paper_trading(broker)
    errors = []
    broker_orders = list(broker.get_orders())
    broker_trades = list(broker.get_trades())
    local_orders = repository.list_orders(account_id=account_id)
    local_fills = repository.list_fills()
    signal_rows = repository.db.query(
        "SELECT status FROM signals ORDER BY created_at"
    )
    converted_signals = sum(
        1 for item in signal_rows if item["status"] == "converted"
    )
    local_order_broker_ids = {
        str(item["broker_order_id"])
        for item in local_orders if item.get("broker_order_id")
    }
    local_trade_ids = {
        str(item["broker_trade_id"])
        for item in local_fills if item.get("broker_trade_id")
    }
    missing_orders = [
        str(item.broker_order_id) for item in broker_orders
        if str(item.broker_order_id) not in local_order_broker_ids
    ]
    missing_fills = [
        str(item.trade_id) for item in broker_trades
        if str(item.trade_id) not in local_trade_ids
    ]
    if missing_orders:
        errors.append(f"券商有 {len(missing_orders)} 笔订单未映射到本地")
    if missing_fills:
        errors.append(f"券商有 {len(missing_fills)} 笔成交未写入本地")
    if signal_rows and converted_signals == 0:
        errors.append("至少存在一个信号，但没有任何信号转换为订单")

    reconciliation = None
    if reconciliation_service is not None:
        reconciliation = reconciliation_service.reconcile(account_id)
        if reconciliation.status != "matched":
            errors.append(
                f"对账未通过：{reconciliation.difference_count} 项差异"
            )

    checks = {
        "safety": safety,
        "signal_count": len(signal_rows),
        "converted_signal_count": converted_signals,
        "order_count": len(local_orders),
        "broker_order_count": len(broker_orders),
        "fill_count": len(local_fills),
        "broker_trade_count": len(broker_trades),
        "missing_orders": missing_orders,
        "missing_fills": missing_fills,
        "risk_event_count": len(
            repository.list_risk_events(account_id)
        ),
        "reconciliation_status": (
            reconciliation.status if reconciliation else "not_run"
        ),
        "reconciliation_difference_count": (
            reconciliation.difference_count if reconciliation else None
        ),
    }
    return LinkVerificationReport(
        passed=not errors,
        checks=checks,
        errors=errors,
    )
