"""结构化日志、监控、告警、配置、备份和守护测试。"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops.alerts import AlertManager
from ops.backup import create_backup, restore_backup, verify_backup
from ops.config import load_profile
from ops.logging_setup import configure_logging, log_event
from ops.metrics import MetricsRegistry
from ops.metrics import start_metrics_server
from ops.redaction import redact, redact_text
from ops.supervisor import ProcessSupervisor
from scripts.package_release import build_release
from scripts.macos.build_app import build_app


class FakeNotifier:
    def __init__(self):
        self.messages = []

    def send_risk_alert(self, message):
        self.messages.append(message)
        return {"fake": True}


class FakeProcess:
    next_pid = 100

    def __init__(self, exit_codes):
        self.exit_codes = list(exit_codes)
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.terminated = False

    def poll(self):
        if not self.exit_codes:
            return 0
        return self.exit_codes.pop(0)

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


def test_redaction_and_structured_logging():
    payload = redact({
        "password": "supersecret",
        "webhook": "https://example.com/hook?key=abcdef123456",
        "account_id": "66001234",
        "normal": "visible",
    }, account=True)
    assert payload["password"] != "supersecret"
    assert "abcdef123456" not in payload["webhook"]
    assert payload["account_id"] == "66***34"
    assert "token=***" in redact_text("token=abcdef")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "runtime.jsonl"
        configure_logging(
            log_file=path,
            json_logs=True,
            force=True,
        )
        logger = logging.getLogger("ops.test")
        log_event(
            logger,
            "test_event",
            "测试日志",
            account_id="66001234",
            api_key="secret-value",
        )
        logging.shutdown()
        line = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
        assert line["event"] == "test_event"
        assert line["context"]["account_id"] == "66***34"
        assert line["context"]["api_key"] != "secret-value"
        configure_logging(force=True)
    print("PASS redaction and structured logging")


def test_metrics_registry():
    registry = MetricsRegistry()
    registry.counter("orders_total", 2, side="buy")
    registry.gauge("runtime_online", 1, account_id="A1")
    registry.histogram("latency_seconds", 0.25)
    text = registry.prometheus()
    assert 'orders_total{side="buy"} 2.0' in text
    assert 'runtime_online{account_id="A1"} 1.0' in text
    assert "latency_seconds_count" in text
    print("PASS metrics registry and Prometheus output")


def test_metrics_http_endpoint():
    from urllib.request import urlopen

    registry = MetricsRegistry()
    registry.gauge("health_value", 1)
    try:
        server = start_metrics_server(
            "127.0.0.1", 0, registry=registry
        )
    except PermissionError:
        print("SKIP metrics HTTP endpoint: sandbox does not allow socket bind")
        return
    try:
        host, port = server.server_address[:2]
        with urlopen(f"http://{host}:{port}/metrics", timeout=5) as response:
            body = response.read().decode("utf-8")
        assert "health_value 1.0" in body
        with urlopen(f"http://{host}:{port}/health", timeout=5) as response:
            assert b'"status":"ok"' in response.read()
    finally:
        server.shutdown()
        server.server_close()
    print("PASS metrics HTTP endpoint")


def test_log_rotation():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "runtime.jsonl"
        configure_logging(
            log_file=path,
            json_logs=True,
            max_bytes=1024,
            backup_count=2,
            force=True,
        )
        logger = logging.getLogger("ops.rotation")
        for index in range(80):
            log_event(logger, "rotation", "x" * 80, index=index)
        logging.shutdown()
        assert path.exists()
        assert Path(str(path) + ".1").exists()
        configure_logging(force=True)
    print("PASS log rotation")


def test_alert_cooldown_and_redaction():
    import datetime as dt

    notifier = FakeNotifier()
    state = Path(tempfile.mkdtemp()) / "alerts.json"
    now = dt.datetime(2026, 9, 14, 10, 0, 0)
    manager = AlertManager(
        notifier,
        cooldown_seconds=60,
        state_file=state,
        clock=lambda: now,
    )
    first = manager.send(
        "DISCONNECT",
        "连接断开",
        context={"account_id": "66001234", "api_key": "secret"},
    )
    second = manager.send("DISCONNECT", "连接断开")
    assert first["sent"] is True
    assert second["sent"] is False
    assert len(notifier.messages) == 1
    assert notifier.messages[0]["account_id"] == "66***34"
    assert notifier.messages[0]["api_key"] != "secret"
    print("PASS alert cooldown and redaction")


def test_config_isolation():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paper = root / "paper"
        paper.mkdir()
        (paper / "common.env").write_text(
            "TRADING_STAGE=paper\nA=common\n",
            encoding="utf-8",
        )
        (paper / "SIM-001.env").write_text(
            "A=account\nQMT_ACCOUNT=SIM-001\n",
            encoding="utf-8",
        )
        env = {}
        profile = load_profile(
            "paper",
            "SIM-001",
            config_root=root,
            environ=env,
        )
        assert env["A"] == "account"
        assert env["QMT_ACCOUNT"] == "SIM-001"
        assert profile.public_dict()["values"]["QMT_ACCOUNT"] == "SI***01"
        try:
            load_profile("../paper", config_root=root, environ={})
            raise AssertionError("路径跳转必须被拒绝")
        except ValueError:
            pass
    print("PASS profile/account config isolation")


def test_sqlite_backup_verify_and_restore():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        database = root / "trading.db"
        conn = sqlite3.connect(database)
        conn.execute("CREATE TABLE sample(value TEXT)")
        conn.execute("INSERT INTO sample VALUES ('before')")
        conn.commit()
        conn.close()
        manifest = create_backup(
            database,
            root / "backups",
            label="test",
        )
        verified = verify_backup(manifest["backup"])
        assert verified["verified"] is True
        conn = sqlite3.connect(database)
        conn.execute("UPDATE sample SET value = 'after'")
        conn.commit()
        conn.close()
        restored = restore_backup(
            manifest["backup"],
            database,
            confirmed=True,
        )
        assert restored["restored"] is True
        conn = sqlite3.connect(database)
        assert conn.execute("SELECT value FROM sample").fetchone()[0] == "before"
        conn.close()
    print("PASS SQLite backup verify and restore")


def test_supervisor_restarts_failed_child():
    processes = []

    def factory(*args, **kwargs):
        process = FakeProcess([1] if not processes else [0])
        processes.append(process)
        return process

    supervisor = ProcessSupervisor(
        ["fake-child"],
        max_restarts=2,
        initial_backoff=0,
        max_backoff=0,
        sleeper=lambda _: None,
        process_factory=factory,
    )
    result = supervisor.run()
    assert result == 0
    assert len(processes) == 2
    assert len(supervisor.restarts) == 1
    print("PASS supervisor restarts failed child")


def test_release_package_excludes_secrets_and_runtime_data():
    with tempfile.TemporaryDirectory() as tmp:
        result = build_release("v-test", tmp)
        with zipfile.ZipFile(result["archive"]) as package:
            names = package.namelist()
        assert "release-manifest.json" in names
        assert "checksums.sha256" in names
        assert not any(name.endswith(".env") for name in names)
        assert not any(name.endswith((".db", ".sqlite", ".sqlite3")) for name in names)
        assert not any("__pycache__" in name for name in names)
    print("PASS release package excludes secrets and runtime data")


def test_macos_app_bundle_builds():
    with tempfile.TemporaryDirectory() as tmp:
        result = build_app(tmp, name="测试软件")
        app = Path(result["app"])
        executable = app / "Contents/MacOS/QuantTradingDesk"
        plist = app / "Contents/Info.plist"
        command = Path(result["command"])
        assert executable.exists()
        assert plist.exists()
        assert command.exists()
        assert "launcher.py" in executable.read_text(encoding="utf-8")
        assert os.access(executable, os.X_OK)
    print("PASS macOS app bundle builds")


def run_test():
    for test in (
        test_redaction_and_structured_logging,
        test_metrics_registry,
        test_metrics_http_endpoint,
        test_log_rotation,
        test_alert_cooldown_and_redaction,
        test_config_isolation,
        test_sqlite_backup_verify_and_restore,
        test_supervisor_restarts_failed_child,
        test_release_package_excludes_secrets_and_runtime_data,
        test_macos_app_bundle_builds,
    ):
        test()
    print("===== ops tests passed =====")


if __name__ == "__main__":
    run_test()
