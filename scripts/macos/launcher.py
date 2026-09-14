"""macOS 应用启动器：启动本地 Web 服务并打开浏览器。"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def build_parser():
    parser = argparse.ArgumentParser(description="量化交易台启动器")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--app-mode", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    root = Path(os.getenv("QUANT_PROJECT_ROOT", PROJECT_ROOT)).resolve()
    runtime_dir = root / "runtime"
    log_dir = root / "logs"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    if args.demo:
        database = runtime_dir / "demo_paper.db"
        _seed_demo(database)
    else:
        database = Path(
            os.getenv("ABACKTEST_TRADING_DB", root / "data/paper_trading.db")
        )
    port = args.port or _free_port()
    url = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env.update({
        "ABACKTEST_TRADING_DB": str(database),
        "TRADING_STAGE": "paper",
        "QMT_TRADING_MODE": "SIMULATION",
        "QMT_ALLOW_REAL_TRADING": "false",
        "OPS_LOG_FILE": str(log_dir / "desktop.jsonl"),
        "OPS_LOG_JSON": "true",
        "PYTHONUNBUFFERED": "1",
        "QUANT_PROJECT_ROOT": str(root),
    })
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(root / "app" / "research_console.py"),
        "--server.address",
        "127.0.0.1",
        "--server.port",
        str(port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    stdout = (log_dir / "desktop.out.log").open("a", encoding="utf-8")
    stderr = (log_dir / "desktop.err.log").open("a", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=str(root),
        env=env,
        stdout=stdout,
        stderr=stderr,
        text=True,
    )
    state_file = log_dir / "launcher.json"
    _write_json(state_file, {
        "status": "starting",
        "pid": process.pid,
        "url": url,
        "database": str(database),
        "started_at": dt.datetime.now().isoformat(),
        "demo": bool(args.demo),
    })
    heartbeat_stop = threading.Event()
    heartbeat = None
    if args.demo:
        heartbeat = threading.Thread(
            target=_demo_heartbeat,
            args=(database, heartbeat_stop),
            daemon=True,
        )
        heartbeat.start()

    def stop(signum=None, frame=None):
        heartbeat_stop.set()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        _write_json(state_file, {
            "status": "stopped",
            "url": url,
            "database": str(database),
            "stopped_at": dt.datetime.now().isoformat(),
        })
        stdout.close()
        stderr.close()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    ready = _wait_ready(f"{url}/_stcore/health", process)
    if not ready:
        stop()
        return 1
    _write_json(state_file, {
        "status": "running",
        "pid": process.pid,
        "url": url,
        "database": str(database),
        "started_at": dt.datetime.now().isoformat(),
        "demo": bool(args.demo),
    })
    if not args.no_browser:
        webbrowser.open(url)
    try:
        return process.wait()
    except KeyboardInterrupt:
        stop()
        return 0


def _wait_ready(url, process, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_json(path, payload):
    temp = path.with_suffix(".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(path)


def _seed_demo(database):
    from persistence import SQLiteDatabase, TradingRepository
    from risk import TradingState

    database = Path(database)
    repo = TradingRepository(SQLiteDatabase(database))
    now = dt.datetime.now()
    repo.save_cash_current(
        account_id="DEMO-PAPER-001",
        total_asset=1_023_600.0,
        available_cash=623_600.0,
        frozen_cash=0.0,
        market_value=400_000.0,
        source="DEMO",
        payload={"account_id": "DEMO-PAPER-001", "demo": True},
        updated_at=now,
    )
    for symbol, quantity, cost, price in (
        ("600519", 100, 1_850.0, 1_920.0),
        ("000001", 10_000, 10.20, 11.80),
    ):
        repo.save_position_current(
            account_id="DEMO-PAPER-001",
            symbol=symbol,
            total_quantity=quantity,
            available_quantity=quantity,
            average_cost=cost,
            market_price=price,
            market_value=quantity * price,
            source="DEMO",
            payload={"symbol": symbol, "demo": True},
            updated_at=now,
        )
    repo.save_order(
        local_order_id="demo-order-001",
        intent_id="demo-intent-001",
        account_id="DEMO-PAPER-001",
        broker_order_id="DEMO-BROKER-001",
        client_order_id="demo-client-001",
        symbol="600519",
        side="buy",
        status="partially_filled",
        requested_quantity=200,
        filled_quantity=100,
        payload={"symbol": "600519", "demo": True},
        updated_at=now,
    )
    repo.save_fill(
        fill_id="demo-fill-001",
        broker_trade_id="DEMO-TRADE-001",
        local_order_id="demo-order-001",
        broker_order_id="DEMO-BROKER-001",
        symbol="600519",
        side="buy",
        filled_quantity=100,
        fill_price=1_920.0,
        total_fee=48.0,
        filled_at=now,
        payload={"trade_id": "DEMO-TRADE-001", "demo": True},
    )
    repo.save_risk_state(
        account_id="DEMO-PAPER-001",
        state=TradingState.ACTIVE,
        state_reason="演示账户",
        payload={"state": "active", "demo": True},
        updated_at=now,
    )
    repo.save_runtime_heartbeat(
        account_id="DEMO-PAPER-001",
        runtime_id="desktop-demo",
        status="confirmed",
        environment="paper",
        payload={"risk_state": "active", "demo": True},
        last_cycle_at=now,
    )
    repo.close()


def _demo_heartbeat(database, stop_event):
    while not stop_event.wait(10):
        try:
            conn = sqlite3.connect(database)
            now = dt.datetime.now().isoformat()
            payload = json.dumps(
                {"risk_state": "active", "demo": True},
                ensure_ascii=False,
            )
            conn.execute("""
                UPDATE runtime_heartbeats
                SET last_cycle_at = ?, payload_json = ?
                WHERE account_id = 'DEMO-PAPER-001'
            """, (now, payload))
            conn.commit()
            conn.close()
        except sqlite3.Error:
            continue


if __name__ == "__main__":
    raise SystemExit(main())
