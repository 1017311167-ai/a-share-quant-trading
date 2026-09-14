"""网页主控制台的双工作区离线冒烟测试。"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _seed_database(path):
    from persistence import SQLiteDatabase, TradingRepository
    from risk import TradingState

    repo = TradingRepository(SQLiteDatabase(path))
    repo.save_cash_current(
        account_id="PAPER-001",
        total_asset=1_000_500.0,
        available_cash=800_500.0,
        frozen_cash=0.0,
        market_value=200_000.0,
        source="BROKER",
        payload={"account_id": "PAPER-001"},
    )
    repo.save_position_current(
        account_id="PAPER-001",
        symbol="600519",
        total_quantity=100,
        available_quantity=100,
        frozen_quantity=0,
        average_cost=1_900.0,
        market_price=2_000.0,
        market_value=200_000.0,
        source="BROKER",
        payload={"symbol": "600519"},
    )
    repo.save_order(
        local_order_id="ui-order-1",
        intent_id="ui-intent-1",
        account_id="PAPER-001",
        broker_order_id="B-UI-1",
        client_order_id="ui-client-1",
        symbol="600519",
        side="buy",
        status="submitted",
        requested_quantity=100,
        filled_quantity=0,
        payload={"symbol": "600519"},
    )
    repo.save_risk_state(
        account_id="PAPER-001",
        state=TradingState.ACTIVE,
        state_reason="",
        payload={"state": "active"},
    )
    repo.save_runtime_heartbeat(
        account_id="PAPER-001",
        runtime_id="ui-runtime",
        status="confirmed",
        environment="paper",
        last_cycle_at=dt.datetime.now(),
        payload={"risk_state": "active"},
    )
    repo.save_config_version(
        config_name="execution",
        version="exec-ui-v1",
        payload={"mode": "paper"},
    )
    repo.close()


def test_trade_desk_and_research_workspace_render():
    from streamlit.testing.v1 import AppTest

    with tempfile.TemporaryDirectory() as tmp:
        path = f"{tmp}/ui.db"
        _seed_database(path)
        env = {
            "ABACKTEST_TRADING_DB": path,
            "TRADING_STAGE": "paper",
            "QMT_TRADING_MODE": "SIMULATION",
            "QMT_ALLOW_REAL_TRADING": "false",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            app = AppTest.from_file(
                os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "research_console.py",
                ),
                default_timeout=30,
            )
            app.run()
            assert not app.exception, app.exception
            markdown = "\n".join(item.value for item in app.markdown)
            assert "模拟盘" in markdown
            assert "禁止真实资金" in markdown

            next(
                item for item in app.button
                if item.label == "全局急停并撤单"
            ).click().run()
            assert not app.exception, app.exception
            next(
                item for item in app.checkbox
                if item.key == "confirm_runtime_command_kill_switch"
            ).check().run()
            next(
                item for item in app.button
                if item.label == "确认执行"
            ).click().run()
            assert not app.exception, app.exception
            assert _pending_command_count(path) == 1

            app.radio(key="workspace_selector").set_value("研究工作台").run()
            assert not app.exception, app.exception
            headings = "\n".join(item.value for item in app.subheader)
            markdown = "\n".join(item.value for item in app.markdown)
            assert "研究总览" in headings
            assert "运行配置" in markdown
            assert "数据质量" in markdown
    print("PASS trade desk and research workspace render")


def _pending_command_count(path):
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        return conn.execute("""
            SELECT COUNT(*) FROM runtime_commands
            WHERE command_type = 'kill_switch' AND status = 'pending'
        """).fetchone()[0]
    finally:
        conn.close()


def run_test():
    test_trade_desk_and_research_workspace_render()
    print("===== web console tests passed =====")


if __name__ == "__main__":
    run_test()
