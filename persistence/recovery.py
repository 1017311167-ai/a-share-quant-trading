"""启动恢复、人工确认和风险状态恢复流程。"""

from __future__ import annotations

import datetime as dt
import uuid

from persistence.reconciliation import ReconciliationService
from persistence.repository import TradingRepository


UNSAFE_ORDER_STATUSES = {
    "created",
    "pending_submit",
    "submitting",
    "unknown",
    "reconciling",
    "failed",
}


class RecoveryService:
    """确保程序重启后先对账、再人工确认，最后恢复交易。

    RecoveryService 不会在启动检查阶段下单、撤单或追价。只有
    `confirm()` 收到明确的操作人员和说明后，才会恢复执行管理器并打开交易。
    """

    def __init__(
            self,
            repository: TradingRepository,
            broker,
            *,
            execution_manager=None,
            execution_bridge=None,
            reconciliation_service: ReconciliationService | None = None,
            risk_engine=None,
            clock=None,
            required_configs=(),
    ):
        self.repository = repository
        self.broker = broker
        self.execution_manager = execution_manager
        self.execution_bridge = execution_bridge
        self.reconciliation_service = reconciliation_service
        self.risk_engine = risk_engine
        self.clock = clock or dt.datetime.now
        self.required_configs = tuple(required_configs)

    def start(
            self,
            account_id: str,
            *,
            reason: str = "process_startup",
    ) -> dict:
        self._require_text(account_id, "account_id")
        now = self.clock()
        case_id = str(uuid.uuid4())
        if self.risk_engine is not None:
            self.risk_engine.enable_stop_open(
                f"程序启动恢复中：{reason}"
            )
            self._persist_risk_state(account_id)

        checks = {
            "broker_connected": False,
            "broker_error": "",
            "open_difference_count": 0,
            "unsafe_order_count": 0,
            "active_order_count": 0,
            "required_configs": {},
            "reconciliation_run_id": None,
            "reconciliation_status": "not_run",
        }
        self.repository.create_recovery_case(
            recovery_case_id=case_id,
            account_id=account_id,
            status="checking",
            reason=reason,
            checks=checks,
            started_at=now,
            payload={"phase": "startup_checks"},
        )

        try:
            if not getattr(self.broker, "_connected", False):
                self.broker.connect()
            checks["broker_connected"] = True
        except Exception as exc:
            checks["broker_error"] = f"{type(exc).__name__}: {exc}"

        if self.execution_bridge is not None and self.execution_manager is not None:
            try:
                checks["execution_sync"] = self.execution_bridge.sync_manager(
                    self.execution_manager
                )
            except Exception as exc:
                checks["execution_sync_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )

        orders = self.repository.list_orders(
            account_id=account_id, active_only=True
        )
        checks["active_order_count"] = len(orders)
        checks["unsafe_order_count"] = sum(
            1 for item in orders
            if str(item["status"]) in UNSAFE_ORDER_STATUSES
        )

        checks["required_configs"] = {
            name: self.repository.get_active_config(name) is not None
            for name in self.required_configs
        }

        reconciliation = self._run_reconciliation(account_id)
        if reconciliation is not None:
            checks["reconciliation_run_id"] = reconciliation.run_id
            checks["reconciliation_status"] = reconciliation.status
            checks["open_difference_count"] = reconciliation.difference_count
        else:
            checks["open_difference_count"] = len(
                self.repository.list_reconciliation_differences(
                    account_id=account_id, open_only=True
                )
            )
            checks["reconciliation_status"] = (
                "mismatched" if checks["open_difference_count"] else "matched"
            )

        blocking = self._blocking_issues(checks)
        checks["blocking_issues"] = blocking
        status = "blocked" if blocking else "ready_for_confirmation"
        payload = {
            "phase": "awaiting_manual_confirmation" if not blocking else "blocked",
            "manual_confirmation_required": True,
            "active_order_ids": [
                item["local_order_id"] for item in orders
            ],
        }
        return self.repository.update_recovery_case(
            case_id,
            status=status,
            checks=checks,
            ready_at=self.clock() if not blocking else None,
            payload=payload,
        )

    def confirm(
            self,
            recovery_case_id: str,
            *,
            confirmed_by: str,
            note: str,
    ) -> dict:
        self._require_text(confirmed_by, "confirmed_by")
        self._require_text(note, "note")
        case = self.repository.get_recovery_case(recovery_case_id)
        if case["status"] == "confirmed":
            return case
        if case["status"] not in {
            "ready_for_confirmation", "blocked",
        }:
            raise ValueError(f"恢复记录当前不可确认：{case['status']}")
        account_id = case["account_id"]
        checks = dict(case["checks"])

        if not checks.get("broker_connected"):
            try:
                if not getattr(self.broker, "_connected", False):
                    self.broker.connect()
                checks["broker_connected"] = True
                checks["broker_error"] = ""
            except Exception as exc:
                checks["broker_error"] = f"{type(exc).__name__}: {exc}"

        execution_result = self._recover_execution()
        if execution_result is not None:
            checks["execution_recovery"] = execution_result

        if self.execution_bridge is not None and self.execution_manager is not None:
            try:
                checks["execution_sync"] = self.execution_bridge.sync_manager(
                    self.execution_manager
                )
            except Exception as exc:
                checks["execution_sync_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )

        orders = self.repository.list_orders(
            account_id=account_id, active_only=True
        )
        checks["active_order_count"] = len(orders)
        checks["unsafe_order_count"] = sum(
            1 for item in orders
            if str(item["status"]) in UNSAFE_ORDER_STATUSES
        )

        reconciliation = self._run_reconciliation(account_id)
        if reconciliation is not None:
            checks["reconciliation_run_id"] = reconciliation.run_id
            checks["reconciliation_status"] = reconciliation.status
            checks["open_difference_count"] = reconciliation.difference_count
        else:
            checks["open_difference_count"] = len(
                self.repository.list_reconciliation_differences(
                    account_id=account_id, open_only=True
                )
            )

        blocking = self._blocking_issues(checks)
        checks["blocking_issues"] = blocking
        if blocking:
            return self.repository.update_recovery_case(
                recovery_case_id,
                status="blocked",
                checks=checks,
                payload={
                    "phase": "confirmation_rejected",
                    "confirmed_by": confirmed_by,
                    "note": note,
                },
            )

        if self.risk_engine is not None:
            self.risk_engine.recover(
                reason=f"人工确认恢复：{confirmed_by}；{note}"
            )
            self._persist_risk_state(account_id)
        confirmed_at = self.clock()
        result = self.repository.update_recovery_case(
            recovery_case_id,
            status="confirmed",
            checks=checks,
            ready_at=case.get("ready_at") or confirmed_at,
            confirmed_at=confirmed_at,
            confirmed_by=confirmed_by,
            confirmation_note=note,
            payload={
                "phase": "runtime_recovered",
                "active_order_ids": [
                    item["local_order_id"] for item in orders
                ],
            },
        )
        self.repository.append_audit_event(
            audit_event_id=str(uuid.uuid4()),
            account_id=account_id,
            aggregate_type="recovery_case",
            aggregate_id=recovery_case_id,
            event_type="manual_recovery_confirmed",
            payload={
                "confirmed_by": confirmed_by,
                "note": note,
                "checks": checks,
            },
            created_at=confirmed_at,
        )
        return result

    def _recover_execution(self):
        if self.execution_manager is None:
            return None
        try:
            orders = self.execution_manager.recover(now=self.clock())
            return {
                "ok": True,
                "order_count": len(orders),
                "orders": [item.to_dict() for item in orders],
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _run_reconciliation(self, account_id):
        if self.reconciliation_service is None:
            return None
        return self.reconciliation_service.reconcile(account_id)

    @staticmethod
    def _blocking_issues(checks):
        issues = []
        if not checks.get("broker_connected"):
            issues.append("券商连接未恢复")
        if checks.get("broker_error"):
            issues.append("券商连接错误")
        if checks.get("open_difference_count", 0):
            issues.append("存在未处理账户差异")
        if checks.get("reconciliation_status") in {"failed", "mismatched"}:
            issues.append("对账未通过")
        if checks.get("unsafe_order_count", 0):
            issues.append("存在未安全恢复的订单")
        execution = checks.get("execution_recovery")
        if execution is not None and not execution.get("ok"):
            issues.append("执行管理器恢复失败")
        if checks.get("execution_sync_error"):
            issues.append("统一订单同步失败")
        missing = [
            name for name, active in checks.get("required_configs", {}).items()
            if not active
        ]
        if missing:
            issues.append(f"缺少活动配置：{'、'.join(missing)}")
        return issues

    def _persist_risk_state(self, account_id):
        if self.risk_engine is None:
            return
        self.repository.save_risk_state(
            account_id=account_id,
            state=self.risk_engine.state,
            state_reason=self.risk_engine.state_reason,
            connected=self.risk_engine.connected,
            policy_hash=self.risk_engine.policy_hash,
            payload=self.risk_engine.snapshot(),
        )

    @staticmethod
    def _require_text(value, name):
        if not str(value or "").strip():
            raise ValueError(f"{name} 不能为空")
