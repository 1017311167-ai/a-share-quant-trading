"""QMT 模拟盘持续运行入口。

示例：
    python3 -m trading.runner --broker mock --bootstrap \
        --operator local-test --signal-file signals.jsonl --max-cycles 3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

from broker_adapter.mock_broker import MockBroker
from broker_adapter.qmt_adapter import QmtBroker
from execution import ExecutionPolicy, OrderExecutionManager, SQLiteExecutionStore
from notification import Notifier
from ops.alerts import AlertManager
from ops.logging_setup import configure_logging, log_event
from ops.metrics import start_metrics_server
from persistence import (
    ExecutionPersistenceBridge,
    ReconciliationService,
    RecoveryService,
    RiskEventPersistenceSink,
    SQLiteDatabase,
    TradingRepository,
)
from risk import RiskEngine, RiskLimits
from trading.feed import JsonlSignalFeed
from trading.runtime import PaperTradingRuntime
from trading.safety import assert_paper_trading
from trading.verification import verify_paper_link


def build_parser():
    parser = argparse.ArgumentParser(description="QMT 模拟盘持续运行")
    parser.add_argument("--broker", choices=("qmt", "mock"), default="qmt")
    parser.add_argument("--account")
    parser.add_argument("--database", default="data/paper_trading.db")
    parser.add_argument("--signal-file")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--reconcile-interval", type=float, default=300.0)
    parser.add_argument("--max-cycles", type=int)
    parser.add_argument(
        "--mock-clock",
        help="仅 MockBroker 使用的固定 ISO 时间，用于非交易时段离线烟测",
    )
    parser.add_argument(
        "--mock-quote",
        action="append",
        default=[],
        help="仅 MockBroker 使用的报价，格式 600519=10.00，可重复",
    )
    parser.add_argument("--duration", type=float)
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--operator")
    parser.add_argument("--note", default="已核对 QMT 模拟账户，同意恢复")
    parser.add_argument(
        "--mock-fill-mode",
        choices=("none", "full", "partial", "reject"),
        default="none",
    )
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--metrics-host", default="127.0.0.1")
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=int(os.getenv("OPS_METRICS_PORT", "0")),
    )
    parser.add_argument("--log-file", default=None)
    parser.add_argument("--json-logs", action="store_true")
    return parser


def build_runtime(args, *, alert_manager=None):
    if args.broker == "qmt":
        broker = QmtBroker(
            account_id=args.account,
            allow_real_trading=False,
        )
    else:
        broker = MockBroker(
            account_id=args.account or "MOCK-001",
            fill_mode=args.mock_fill_mode,
            initial_cash=1_000_000.0,
            t_plus_one=True,
        )
    safety = assert_paper_trading(broker)
    clock = None
    if args.broker == "mock" and args.mock_clock:
        import datetime as dt

        fixed_time = dt.datetime.fromisoformat(args.mock_clock)
        clock = lambda: fixed_time

    path = Path(args.database)
    repository = TradingRepository(SQLiteDatabase(path), clock=clock)
    execution_store = SQLiteExecutionStore(path)
    account_id = args.account or getattr(broker, "account_id", "") or "MOCK-001"
    limits = RiskLimits(
        max_single_position_pct=0.20,
        max_gross_exposure_pct=0.60,
        min_cash_pct=0.20,
        max_daily_loss_pct=0.02,
        max_drawdown_pct=0.05,
        max_quote_age_seconds=60,
        max_orders_per_day=20,
        latest_entry_time="14:50",
    )
    repository.save_config_version(
        config_name="execution",
        version=ExecutionPolicy().policy_hash,
        payload={"policy": "default-paper"},
    )
    repository.save_config_version(
        config_name="risk",
        version=limits.policy_hash,
        payload=limits.__dict__,
    )
    risk_engine = RiskEngine(limits)
    risk_sink = RiskEventPersistenceSink(
        repository,
        account_id=account_id,
        engine=lambda: risk_engine,
    )
    risk_engine.event_sink = risk_sink
    execution_manager = OrderExecutionManager(
        broker,
        execution_store,
        policy=ExecutionPolicy(),
        risk_engine=risk_engine,
        require_risk=True,
        clock=clock,
    )
    bridge = ExecutionPersistenceBridge(
        repository,
        account_id=account_id,
        t_plus_one=True,
    )
    reconciliation = ReconciliationService(
        repository,
        broker,
        risk_engine=risk_engine,
        notifier=Notifier() if args.notify else None,
        clock=clock,
    )
    recovery = RecoveryService(
        repository,
        broker,
        execution_manager=execution_manager,
        execution_bridge=bridge,
        reconciliation_service=reconciliation,
        risk_engine=risk_engine,
        required_configs=("execution", "risk"),
        clock=clock,
    )
    feed = JsonlSignalFeed(args.signal_file) if args.signal_file else None
    runtime = PaperTradingRuntime(
        repository=repository,
        broker=broker,
        account_id=account_id,
        risk_engine=risk_engine,
        execution_manager=execution_manager,
        execution_bridge=bridge,
        reconciliation_service=reconciliation,
        recovery_service=recovery,
        signal_feed=feed,
        poll_interval=args.poll_interval,
        reconcile_interval=args.reconcile_interval,
        clock=clock,
        alert_manager=alert_manager,
    )
    return runtime, repository, execution_store, safety


def main(argv=None):
    args = build_parser().parse_args(argv)
    configure_logging(
        log_file=args.log_file,
        json_logs=True if args.json_logs else None,
    )
    logger = configure_logging().getChild("runner")
    alert_manager = AlertManager(
        Notifier() if args.notify else None,
        cooldown_seconds=float(
            os.getenv("OPS_ALERT_COOLDOWN_SECONDS", "300")
        ),
        state_file=os.getenv(
            "OPS_ALERT_STATE_FILE", "logs/alert_state.json"
        ),
    )
    metrics_server = None
    if args.metrics_port > 0:
        metrics_server = start_metrics_server(
            args.metrics_host, args.metrics_port
        )
    runtime, repository, execution_store, safety = build_runtime(
        args, alert_manager=alert_manager
    )
    account_id = runtime.account_id
    log_event(
        logger,
        "runner_started",
        "模拟盘运行入口已启动",
        account_id=account_id,
        broker=args.broker,
        database=str(repository.db.path),
        metrics_port=args.metrics_port,
    )
    try:
        if not getattr(runtime.broker, "_connected", False):
            runtime.broker.connect()
        if args.bootstrap:
            if not args.operator:
                raise SystemExit("--bootstrap 必须同时提供 --operator")
            runtime.bootstrap_account(
                operator=args.operator,
                note="QMT 模拟盘首次运行基线",
            )
        case = runtime.start_recovery()
        if case["status"] == "ready_for_confirmation" and args.operator:
            case = runtime.confirm_recovery(
                confirmed_by=args.operator,
                note=args.note,
            )
        if case["status"] != "confirmed":
            print(json.dumps(case, ensure_ascii=False, indent=2))
            print(
                "模拟盘未启动：必须先处理恢复检查，并由 --operator 人工确认。",
                file=sys.stderr,
            )
            return 2
        symbols = [
            item.strip() for item in args.symbols.split(",") if item.strip()
        ]
        if symbols:
            runtime.broker.subscribe_realtime(symbols, runtime.on_quote)
        for item in args.mock_quote:
            if args.broker != "mock":
                raise SystemExit("--mock-quote 只能用于 MockBroker")
            try:
                symbol, price = item.split("=", 1)
                price = float(price)
            except ValueError as exc:
                raise SystemExit(
                    f"--mock-quote 格式错误：{item!r}，应为 600519=10.00"
                ) from exc
            runtime.on_quote(symbol.strip(), {
                "lastPrice": price,
                "lastClose": price,
                "limitUp": round(price * 1.1, 2),
                "limitDown": round(price * 0.9, 2),
                "timestamp": runtime.clock(),
            })
        stop_event = None
        if args.duration:
            import threading

            stop_event = threading.Event()

            def stop_later():
                time.sleep(max(0.0, args.duration))
                stop_event.set()

            threading.Thread(target=stop_later, daemon=True).start()
        cycles = runtime.run_forever(
            stop_event=stop_event, max_cycles=args.max_cycles
        )
        final = runtime.process_once()
        verification = verify_paper_link(
            repository,
            runtime.broker,
            account_id,
            reconciliation_service=runtime.reconciliation_service,
        )
        repository.append_audit_event(
            audit_event_id=str(uuid.uuid4()),
            account_id=account_id,
            aggregate_type="paper_runtime",
            aggregate_id=account_id,
            event_type="runtime_stopped",
            payload={
                "safety": safety,
                "cycles": cycles,
                "final": final,
            },
        )
        print(json.dumps({
            "安全模式": safety,
            "账户": account_id,
            "运行轮次": cycles,
            "最终状态": runtime.state,
            "数据库": str(repository.db.path),
            "链路验收": verification.to_dict(),
        }, ensure_ascii=False, indent=2))
        log_event(
            logger,
            "runner_stopped",
            "模拟盘运行入口已停止",
            account_id=account_id,
            cycles=cycles,
            verification_passed=verification.passed,
        )
        return 0 if verification.passed else 1
    finally:
        runtime.broker.disconnect()
        repository.close()
        execution_store.close()
        if metrics_server is not None:
            metrics_server.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
