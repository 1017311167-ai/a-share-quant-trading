"""每日账户对账、差异归档和风险降级。"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from broker_adapter.models import normalize_symbol
from persistence.repository import TradingRepository


POSITION_DIFF_TYPES = {
    "POSITION_MISSING_LOCAL",
    "POSITION_MISSING_BROKER",
    "POSITION_QUANTITY_MISMATCH",
}
CASH_DIFF_TYPES = {
    "CASH_MISSING_LOCAL",
    "CASH_MISMATCH",
}


@dataclass(frozen=True)
class ReconciliationResult:
    run_id: str
    account_id: str
    status: str
    difference_count: int
    differences: tuple[dict, ...] = ()
    alert_result: dict = field(default_factory=dict)


class ReconciliationService:
    """以券商为事实源，核对本地订单、成交、持仓和资金。"""

    def __init__(
            self,
            repository: TradingRepository,
            broker,
            *,
            risk_engine=None,
            notifier=None,
            clock=None,
            cash_tolerance: float = 0.01,
    ):
        self.repository = repository
        self.broker = broker
        self.risk_engine = risk_engine
        self.notifier = notifier
        self.clock = clock or dt.datetime.now
        self.cash_tolerance = float(cash_tolerance)

    def run_daily(
            self,
            account_id: str | None = None,
            *,
            trade_date=None,
    ) -> ReconciliationResult:
        """执行一次每日对账；重复运行会保留每次审计结果。"""
        return self.reconcile(account_id, trade_date=trade_date)

    def reconcile(
            self,
            account_id: str | None = None,
            *,
            trade_date=None,
    ) -> ReconciliationResult:
        now = self.clock()
        trade_date = trade_date or now.date()
        run_id = str(uuid.uuid4())
        try:
            account_info = self.broker.get_account_info()
            account_id = account_id or _account_id(account_info, self.broker)
            self.repository.start_reconciliation_run(
                reconciliation_run_id=run_id,
                account_id=account_id,
                trade_date=trade_date,
                started_at=now,
                payload={"source": "broker"},
            )
            broker_positions = _broker_positions(self.broker.get_positions())
            local_positions = self.repository.list_positions_current(account_id)
            local_cash = self.repository.get_cash_current(account_id)
            broker_orders = list(self.broker.get_orders())
            broker_trades = list(self.broker.get_trades())
            local_orders = self.repository.list_orders(account_id=account_id)
            local_fills = self.repository.list_fills()

            new_differences = []
            new_differences.extend(self._compare_positions(
                run_id, account_id, local_positions, broker_positions, now
            ))
            new_differences.extend(self._compare_cash(
                run_id, account_id, local_cash, account_info, now
            ))
            new_differences.extend(self._compare_orders(
                run_id, account_id, local_orders, broker_orders, now
            ))
            new_differences.extend(self._compare_fills(
                run_id, account_id, local_fills, broker_trades, now
            ))

            self._persist_broker_positions(
                account_id, broker_positions, local_positions, now, run_id
            )
            self._persist_broker_cash(
                account_id, account_info, now, run_id
            )

            # 未人工解决的旧差异不能被本轮“当前状态已一致”掩盖。
            differences = self.repository.list_reconciliation_differences(
                account_id=account_id, open_only=True
            )
            status = "mismatched" if differences else "matched"
            alert_result = {}
            if differences:
                if self.risk_engine is not None:
                    self.risk_engine.enable_stop_open(
                        f"账户对账发现 {len(differences)} 项差异，禁止开仓"
                    )
                    self._persist_risk_state(account_id)
                alert_result = self._send_alert(
                    account_id, run_id, trade_date, differences
                )
            self.repository.finish_reconciliation_run(
                run_id,
                status=status,
                difference_count=len(differences),
                finished_at=self.clock(),
                payload={
                    "trade_date": _date_text(trade_date),
                    "broker_order_count": len(broker_orders),
                    "broker_trade_count": len(broker_trades),
                    "local_order_count": len(local_orders),
                    "local_fill_count": len(local_fills),
                    "difference_types": [
                        item["difference_type"] for item in differences
                    ],
                    "new_difference_types": [
                        item["difference_type"] for item in new_differences
                    ],
                    "unresolved_difference_count": len(differences),
                    "alert_result": alert_result,
                },
            )
            return ReconciliationResult(
                run_id=run_id,
                account_id=account_id,
                status=status,
                difference_count=len(differences),
                differences=tuple(differences),
                alert_result=alert_result,
            )
        except Exception as exc:
            existing = self.repository.db.query_one("""
                SELECT reconciliation_run_id FROM reconciliation_runs
                WHERE reconciliation_run_id = ?
            """, (run_id,))
            if existing is not None:
                self.repository.finish_reconciliation_run(
                    run_id,
                    status="failed",
                    difference_count=1,
                    finished_at=self.clock(),
                    payload={
                        "error": f"{type(exc).__name__}: {exc}",
                        "trade_date": _date_text(trade_date),
                    },
                )
            if self.risk_engine is not None and account_id:
                self.risk_engine.enable_stop_open(
                    f"账户对账失败：{type(exc).__name__}: {exc}"
                )
                self._persist_risk_state(account_id)
            if self.notifier is not None and account_id:
                self._safe_alert({
                    "类型": "账户对账失败",
                    "账户": account_id,
                    "交易日期": _date_text(trade_date),
                    "原因": f"{type(exc).__name__}: {exc}",
                    "处理要求": "禁止开仓，检查券商连接与数据后重试",
                })
            raise

    def resolve_difference(
            self,
            difference_id: str,
            *,
            resolved_by: str,
            note: str,
            resolution: str = "manual",
    ) -> dict:
        result = self.repository.resolve_reconciliation_difference(
            difference_id,
            resolved_by=resolved_by,
            note=note,
            resolution=resolution,
        )
        self.repository.append_audit_event(
            audit_event_id=str(uuid.uuid4()),
            account_id=result["account_id"],
            aggregate_type="reconciliation_difference",
            aggregate_id=difference_id,
            event_type="manually_resolved",
            payload={
                "resolved_by": resolved_by,
                "note": note,
                "resolution": resolution,
                "difference_type": result["difference_type"],
            },
        )
        return result

    def resolve_account_with_broker(
            self,
            account_id: str,
            *,
            resolved_by: str,
            note: str,
    ) -> int:
        """接受券商持仓/资金为当前事实，并关闭对应差异。

        订单和成交差异不会在此处静默修复，必须按订单恢复流程单独处理。
        """
        if not str(resolved_by or "").strip():
            raise ValueError("确认券商状态必须提供 resolved_by")
        if not str(note or "").strip():
            raise ValueError("确认券商状态必须提供处理说明")
        self._apply_broker_positions(account_id)
        self._apply_broker_cash(account_id)
        resolved = 0
        for item in self.repository.list_reconciliation_differences(
                account_id=account_id, open_only=True):
            if item["difference_type"] not in POSITION_DIFF_TYPES | CASH_DIFF_TYPES:
                continue
            self.repository.resolve_reconciliation_difference(
                item["difference_id"],
                resolved_by=resolved_by,
                note=note,
                resolution="broker_authoritative",
            )
            resolved += 1
        self.repository.append_audit_event(
            audit_event_id=str(uuid.uuid4()),
            account_id=account_id,
            aggregate_type="account",
            aggregate_id=account_id,
            event_type="broker_state_accepted",
            payload={
                "resolved_by": resolved_by,
                "note": note,
                "resolved_difference_count": resolved,
            },
        )
        return resolved

    def bootstrap_account(
            self,
            account_id: str,
            *,
            resolved_by: str,
            note: str,
    ) -> dict:
        """模拟阶段首次运行前，以券商账户建立本地账本基线。"""
        if not str(resolved_by or "").strip():
            raise ValueError("建立账户基线必须提供 resolved_by")
        if not str(note or "").strip():
            raise ValueError("建立账户基线必须提供处理说明")
        now = self.clock()
        account_info = self.broker.get_account_info()
        broker_positions = _broker_positions(self.broker.get_positions())
        self._persist_broker_positions(
            account_id, broker_positions, [], now, None
        )
        self._persist_broker_cash(
            account_id, account_info, now, None
        )
        for item in self.repository.list_positions_current(account_id):
            self.repository.save_position_current(
                account_id=account_id,
                symbol=item["symbol"],
                total_quantity=item["total_quantity"],
                available_quantity=item["available_quantity"],
                frozen_quantity=item["frozen_quantity"],
                average_cost=item.get("average_cost", 0.0),
                market_price=item.get("market_price", 0.0),
                market_value=item.get("market_value", 0.0),
                source="BOOTSTRAP",
                payload={**item, "source": "BOOTSTRAP"},
                updated_at=now,
            )
        cash = self.repository.get_cash_current(account_id)
        if cash is not None:
            self.repository.save_cash_current(
                account_id=account_id,
                total_asset=cash["total_asset"],
                available_cash=cash["available_cash"],
                frozen_cash=cash["frozen_cash"],
                market_value=cash["market_value"],
                receivable=cash.get("receivable", 0.0),
                payable=cash.get("payable", 0.0),
                source="BOOTSTRAP",
                payload={**cash, "source": "BOOTSTRAP"},
                updated_at=now,
            )
        self.repository.append_audit_event(
            audit_event_id=str(uuid.uuid4()),
            account_id=account_id,
            aggregate_type="account",
            aggregate_id=account_id,
            event_type="paper_account_bootstrapped",
            payload={
                "resolved_by": resolved_by,
                "note": note,
                "position_count": len(broker_positions),
            },
            created_at=now,
        )
        return {
            "account_id": account_id,
            "position_count": len(broker_positions),
            "cash": account_info,
            "bootstrapped_at": now.isoformat(),
        }

    def _compare_positions(
            self, run_id, account_id, local_positions, broker_positions, now):
        local_map = {item["symbol"]: item for item in local_positions}
        broker_map = {item["symbol"]: item for item in broker_positions}
        differences = []
        for symbol in sorted(set(local_map) | set(broker_map)):
            local = local_map.get(symbol)
            broker = broker_map.get(symbol)
            broker_quantity = int((broker or {}).get("total_quantity", 0))
            if local is None and broker_quantity:
                differences.append(self._save_difference(
                    run_id, account_id, "POSITION_MISSING_LOCAL", now,
                    symbol=symbol,
                    local_value=None,
                    broker_value=broker,
                    difference_value=broker_quantity,
                    payload={"symbol": symbol, "broker": broker},
                ))
                continue
            if local is not None and broker is None:
                differences.append(self._save_difference(
                    run_id, account_id, "POSITION_MISSING_BROKER", now,
                    symbol=symbol,
                    local_value=local,
                    broker_value=None,
                    difference_value=-int(local["total_quantity"]),
                    payload={"symbol": symbol, "local": local},
                ))
                continue
            if local is None or broker is None:
                continue
            local_pair = (
                int(local["total_quantity"]),
                int(local["available_quantity"]),
            )
            broker_pair = (
                int(broker["total_quantity"]),
                int(broker["available_quantity"]),
            )
            if local_pair != broker_pair:
                differences.append(self._save_difference(
                    run_id, account_id, "POSITION_QUANTITY_MISMATCH", now,
                    symbol=symbol,
                    local_value=local_pair,
                    broker_value=broker_pair,
                    difference_value=(
                        broker_pair[0] - local_pair[0],
                        broker_pair[1] - local_pair[1],
                    ),
                    payload={"symbol": symbol, "local": local, "broker": broker},
                ))
        return differences

    def _compare_cash(
            self, run_id, account_id, local_cash, broker_cash, now):
        if local_cash is None:
            if _cash_is_zero(broker_cash):
                return []
            return [self._save_difference(
                run_id, account_id, "CASH_MISSING_LOCAL", now,
                local_value=None,
                broker_value=_cash_tuple(broker_cash),
                difference_value=_cash_tuple(broker_cash),
                payload={"broker": broker_cash},
            )]
        local = _cash_tuple(local_cash)
        broker = _cash_tuple(broker_cash)
        deltas = tuple(round(broker[index] - local[index], 6)
                       for index in range(len(local)))
        if any(abs(value) > self.cash_tolerance for value in deltas):
            return [self._save_difference(
                run_id, account_id, "CASH_MISMATCH", now,
                local_value=local,
                broker_value=broker,
                difference_value=deltas,
                payload={
                    "local": local_cash,
                    "broker": broker_cash,
                    "tolerance": self.cash_tolerance,
                },
            )]
        return []

    def _compare_orders(
            self, run_id, account_id, local_orders, broker_orders, now):
        local_by_broker = {
            str(item["broker_order_id"]): item
            for item in local_orders if item.get("broker_order_id")
        }
        local_by_client = {
            str(item["client_order_id"]): item
            for item in local_orders if item.get("client_order_id")
        }
        broker_ids = set()
        differences = []
        for broker_order in broker_orders:
            broker_id = str(broker_order.broker_order_id)
            broker_ids.add(broker_id)
            local = local_by_broker.get(broker_id)
            if local is None and broker_order.client_order_id:
                local = local_by_client.get(str(broker_order.client_order_id))
            if local is None:
                differences.append(self._save_difference(
                    run_id, account_id, "BROKER_ORDER_MISSING_LOCAL", now,
                    symbol=broker_order.symbol,
                    local_value=None,
                    broker_value=broker_order.to_dict(),
                    difference_value=broker_order.status.value,
                    payload={"broker_order": broker_order.to_dict()},
                ))
                continue
            local_filled = int(local.get("filled_quantity", 0))
            if local_filled != int(broker_order.filled_quantity):
                differences.append(self._save_difference(
                    run_id, account_id, "ORDER_FILLED_QUANTITY_MISMATCH", now,
                    symbol=broker_order.symbol,
                    local_value=local_filled,
                    broker_value=int(broker_order.filled_quantity),
                    difference_value=(
                        int(broker_order.filled_quantity) - local_filled
                    ),
                    payload={
                        "local_order": local,
                        "broker_order": broker_order.to_dict(),
                    },
                ))
            if str(local.get("status")) != broker_order.status.value:
                differences.append(self._save_difference(
                    run_id, account_id, "ORDER_STATUS_MISMATCH", now,
                    symbol=broker_order.symbol,
                    local_value=local.get("status"),
                    broker_value=broker_order.status.value,
                    difference_value=broker_order.status.value,
                    payload={
                        "local_order": local,
                        "broker_order": broker_order.to_dict(),
                    },
                ))
        for local in local_orders:
            broker_id = local.get("broker_order_id")
            if broker_id and str(broker_id) in broker_ids:
                continue
            status = str(local.get("status", ""))
            if status in {"cancelled", "rejected", "expired", "failed"}:
                continue
            differences.append(self._save_difference(
                run_id, account_id, "LOCAL_ORDER_MISSING_BROKER", now,
                symbol=local.get("symbol"),
                local_value=local,
                broker_value=None,
                difference_value=status,
                payload={"local_order": local},
            ))
        return differences

    def _compare_fills(
            self, run_id, account_id, local_fills, broker_trades, now):
        local_ids = {
            str(item["broker_trade_id"])
            for item in local_fills if item.get("broker_trade_id")
        }
        differences = []
        for trade in broker_trades:
            if str(trade.trade_id) in local_ids:
                continue
            differences.append(self._save_difference(
                run_id, account_id, "BROKER_FILL_MISSING_LOCAL", now,
                symbol=trade.symbol,
                local_value=None,
                broker_value=trade.to_dict(),
                difference_value=trade.trade_id,
                payload={"broker_trade": trade.to_dict()},
            ))
        return differences

    def _persist_broker_positions(
            self, account_id, broker_positions, local_positions, now, run_id):
        broker_map = {item["symbol"]: item for item in broker_positions}
        local_map = {item["symbol"]: item for item in local_positions}
        for symbol in sorted(set(broker_map) | set(local_map)):
            item = broker_map.get(symbol)
            snapshot = {
                "account_id": account_id,
                "symbol": symbol,
                "total_quantity": int((item or {}).get("total_quantity", 0)),
                "available_quantity": int(
                    (item or {}).get("available_quantity", 0)
                ),
                "frozen_quantity": int((item or {}).get("frozen_quantity", 0)),
                "average_cost": float((item or {}).get("average_cost", 0)),
                "market_price": float((item or {}).get("market_price", 0)),
                "market_value": float((item or {}).get("market_value", 0)),
                "source": "BROKER",
                "snapshot_time": now.isoformat(),
                "reconciliation_run_id": run_id,
            }
            self.repository.save_position_snapshot(
                snapshot_id=str(uuid.uuid4()),
                account_id=account_id,
                symbol=symbol,
                snapshot_time=now,
                total_quantity=snapshot["total_quantity"],
                available_quantity=snapshot["available_quantity"],
                frozen_quantity=snapshot["frozen_quantity"],
                market_value=snapshot["market_value"],
                source="BROKER",
                payload=snapshot,
            )
            self.repository.save_position_current(
                account_id=account_id,
                symbol=symbol,
                total_quantity=snapshot["total_quantity"],
                available_quantity=snapshot["available_quantity"],
                frozen_quantity=snapshot["frozen_quantity"],
                average_cost=snapshot["average_cost"],
                market_price=snapshot["market_price"],
                market_value=snapshot["market_value"],
                source="BROKER",
                payload=snapshot,
                updated_at=now,
            )

    def _persist_broker_cash(self, account_id, account_info, now, run_id):
        snapshot = {
            "account_id": account_id,
            "total_asset": float(account_info.get("总资产", 0.0)),
            "available_cash": float(account_info.get("可用资金", 0.0)),
            "frozen_cash": float(account_info.get("冻结资金", 0.0)),
            "market_value": float(account_info.get("持仓市值", 0.0)),
            "receivable": float(account_info.get("应收资金", 0.0)),
            "payable": float(account_info.get("应付资金", 0.0)),
            "source": "BROKER",
            "snapshot_time": now.isoformat(),
            "reconciliation_run_id": run_id,
        }
        self.repository.save_cash_snapshot(
            cash_snapshot_id=str(uuid.uuid4()),
            account_id=account_id,
            snapshot_time=now,
            total_asset=snapshot["total_asset"],
            available_cash=snapshot["available_cash"],
            frozen_cash=snapshot["frozen_cash"],
            market_value=snapshot["market_value"],
            receivable=snapshot["receivable"],
            payable=snapshot["payable"],
            source="BROKER",
            reconciliation_run_id=run_id,
            payload=snapshot,
        )
        self.repository.save_cash_current(
            account_id=account_id,
            total_asset=snapshot["total_asset"],
            available_cash=snapshot["available_cash"],
            frozen_cash=snapshot["frozen_cash"],
            market_value=snapshot["market_value"],
            receivable=snapshot["receivable"],
            payable=snapshot["payable"],
            source="BROKER",
            payload=snapshot,
            updated_at=now,
        )

    def _apply_broker_positions(self, account_id):
        current_symbols = {
            item["symbol"]
            for item in self.repository.list_positions_current(account_id)
        }
        for item in self.repository.latest_position_snapshots(
                account_id, source="BROKER"):
            current_symbols.add(item["symbol"])
        for symbol in sorted(current_symbols):
            snapshots = [
                item for item in self.repository.latest_position_snapshots(
                    account_id, source="BROKER"
                ) if item["symbol"] == symbol
            ]
            if not snapshots:
                continue
            item = snapshots[0]
            self.repository.save_position_current(
                account_id=account_id,
                symbol=symbol,
                total_quantity=item.get("total_quantity", 0),
                available_quantity=item.get("available_quantity", 0),
                frozen_quantity=item.get("frozen_quantity", 0),
                average_cost=item.get("average_cost", 0),
                market_price=item.get("market_price", 0),
                market_value=item.get("market_value", 0),
                source="RECONCILED",
                payload={**item, "source": "RECONCILED"},
            )

    def _apply_broker_cash(self, account_id):
        row = self.repository.db.query_one("""
            SELECT * FROM cash_snapshots
            WHERE account_id = ? AND source = 'BROKER'
            ORDER BY snapshot_time DESC LIMIT 1
        """, (account_id,))
        if row is None:
            return
        item = dict(row)
        self.repository.save_cash_current(
            account_id=account_id,
            total_asset=item["total_asset"],
            available_cash=item["available_cash"],
            frozen_cash=item["frozen_cash"],
            market_value=item["market_value"],
            receivable=item["receivable"],
            payable=item["payable"],
            source="RECONCILED",
            payload={
                "account_id": account_id,
                "total_asset": item["total_asset"],
                "available_cash": item["available_cash"],
                "frozen_cash": item["frozen_cash"],
                "market_value": item["market_value"],
                "source": "RECONCILED",
            },
        )

    def _save_difference(
            self,
            run_id,
            account_id,
            difference_type,
            now,
            *,
            local_value,
            broker_value,
            difference_value,
            payload,
            symbol=None,
    ):
        for existing in self.repository.list_reconciliation_differences(
                account_id=account_id, open_only=True):
            if (
                existing["difference_type"] == difference_type
                and existing.get("symbol") == symbol
            ):
                return existing
        difference_id = str(uuid.uuid4())
        self.repository.save_reconciliation_difference(
            difference_id=difference_id,
            reconciliation_run_id=run_id,
            account_id=account_id,
            difference_type=difference_type,
            symbol=symbol,
            local_value=local_value,
            broker_value=broker_value,
            difference_value=difference_value,
            payload=payload,
            created_at=now,
        )
        return self.repository.get_reconciliation_difference(difference_id)

    def _send_alert(self, account_id, run_id, trade_date, differences):
        types = sorted({item["difference_type"] for item in differences})
        message = {
            "类型": "每日账户对账差异",
            "账户": account_id,
            "交易日期": _date_text(trade_date),
            "差异数量": len(differences),
            "差异类型": "、".join(types),
            "对账批次": run_id,
            "当前动作": "已进入 STOP_OPEN，禁止开仓",
            "处理要求": "人工核查券商回报与本地账本后确认差异",
        }
        return self._safe_alert(message)

    def _safe_alert(self, message):
        if self.notifier is None:
            return {}
        try:
            return self.notifier.send_risk_alert(message)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

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


def _account_id(account_info, broker):
    return str(
        account_info.get("资金账号")
        or getattr(broker, "account_id", "")
        or "UNKNOWN"
    )


def _broker_positions(items):
    result = []
    for item in items:
        symbol = normalize_symbol(item.get("股票代码", ""))
        if not symbol:
            continue
        result.append({
            "symbol": symbol,
            "name": str(item.get("股票名称", "")),
            "total_quantity": int(item.get("持仓数量", 0)),
            "available_quantity": int(item.get("可用数量", 0)),
            "frozen_quantity": int(item.get("冻结数量", 0)),
            "average_cost": float(item.get("成本价", 0.0)),
            "market_price": float(item.get("最新价", 0.0)),
            "market_value": float(item.get("市值", 0.0)),
            "raw": dict(item),
        })
    return result


def _cash_is_zero(account_info):
    return all(
        abs(float(account_info.get(key, 0.0))) <= 1e-9
        for key in ("总资产", "可用资金", "冻结资金", "持仓市值")
    )


def _cash_tuple(values):
    return (
        float(values.get("总资产", values.get("total_asset", 0.0))),
        float(values.get("可用资金", values.get("available_cash", 0.0))),
        float(values.get("冻结资金", values.get("frozen_cash", 0.0))),
        float(values.get("持仓市值", values.get("market_value", 0.0))),
    )


def _date_text(value) -> str:
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)
