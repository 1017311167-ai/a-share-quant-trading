"""交易台：账户、持仓、订单、成交、策略、风控和急停。"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pandas as pd
import streamlit as st

from persistence import SQLiteDatabase, TradingRepository


DEFAULT_DATABASES = (
    "data/paper_trading.db",
    "data/paper_smoke.db",
    "data/trading.db",
)


def environment_snapshot() -> dict:
    stage = os.getenv("TRADING_STAGE", "paper").strip().lower() or "paper"
    qmt_mode = (
        os.getenv("QMT_TRADING_MODE", "SIMULATION").strip().upper()
        or "SIMULATION"
    )
    allow_real = os.getenv("QMT_ALLOW_REAL_TRADING", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if stage in {"real", "live", "production", "实盘"} or qmt_mode == "REAL":
        kind = "实盘"
        tone = "danger"
        note = "检测到真实资金配置；当前代码基线只允许模拟盘。"
    elif stage == "paper" or qmt_mode in {"SIMULATION", "PAPER"}:
        kind = "模拟盘"
        tone = "paper"
        note = "真实资金交易被程序硬门禁阻止。"
    else:
        kind = "研究环境"
        tone = "neutral"
        note = "尚未声明交易环境。"
    return {
        "kind": kind,
        "tone": tone,
        "stage": stage,
        "qmt_mode": qmt_mode,
        "allow_real_trading": allow_real,
        "note": note,
    }


def discover_database() -> Path | None:
    configured = (
        os.getenv("ABACKTEST_TRADING_DB")
        or os.getenv("TRADING_DATABASE")
    )
    if configured:
        path = Path(configured).expanduser()
        return path if path.exists() else None
    for candidate in DEFAULT_DATABASES:
        path = Path(candidate)
        if path.exists():
            return path
    return None


def render_trading_desk():
    environment = environment_snapshot()
    render_environment_band(environment)
    path = discover_database()
    if path is None:
        st.warning("未找到交易数据库。请先启动 `python3 -m trading.runner`。")
        st.code(
            "python3 -m trading.runner --broker qmt "
            "--database data/paper_trading.db",
            language="bash",
        )
        return
    accounts = _account_ids(path)
    if not accounts:
        st.info(
            f"数据库 `{path}` 已存在，但尚未建立账户基线。"
        )
        return
    default_account = st.session_state.get("trading_desk_account")
    account_id = st.selectbox(
        "交易账户",
        accounts,
        index=(
            accounts.index(default_account)
            if default_account in accounts else 0
        ),
        key="trading_desk_account_select",
    )
    st.session_state["trading_desk_account"] = account_id
    snapshot = load_trading_snapshot(path, account_id)
    _render_account_metrics(snapshot, environment)
    _render_control_panel(path, account_id, snapshot)
    _render_trading_tables(snapshot)


def load_trading_snapshot(path, account_id: str) -> dict:
    with _readonly_connection(path) as conn:
        account = _one(conn, """
            SELECT * FROM cash_current WHERE account_id = ?
        """, (account_id,))
        positions = _all(conn, """
            SELECT * FROM positions_current
            WHERE account_id = ? ORDER BY market_value DESC
        """, (account_id,))
        orders = _all(conn, """
            SELECT * FROM orders WHERE account_id = ?
            ORDER BY updated_at DESC LIMIT 300
        """, (account_id,))
        order_ids = [item["local_order_id"] for item in orders]
        fills = _fills_for_orders(conn, order_ids)
        strategies = _all(conn, """
            SELECT * FROM strategy_instances
            WHERE account_id = ? ORDER BY updated_at DESC
        """, (account_id,))
        risk_state = _one(conn, """
            SELECT * FROM risk_states WHERE account_id = ?
        """, (account_id,))
        risk_events = _all(conn, """
            SELECT * FROM risk_events WHERE account_id = ?
            ORDER BY occurred_at DESC LIMIT 200
        """, (account_id,))
        reconciliation = _one(conn, """
            SELECT * FROM reconciliation_runs WHERE account_id = ?
            ORDER BY started_at DESC LIMIT 1
        """, (account_id,))
        differences = _all(conn, """
            SELECT * FROM reconciliation_differences
            WHERE account_id = ? ORDER BY created_at DESC LIMIT 200
        """, (account_id,))
        commands = _all(conn, """
            SELECT * FROM runtime_commands WHERE account_id = ?
            ORDER BY requested_at DESC LIMIT 100
        """, (account_id,))
        heartbeat = _one(conn, """
            SELECT * FROM runtime_heartbeats WHERE account_id = ?
        """, (account_id,))
        cash_snapshots = _all(conn, """
            SELECT * FROM cash_snapshots WHERE account_id = ?
            ORDER BY snapshot_time DESC LIMIT 200
        """, (account_id,))
        active_configs = _all(conn, """
            SELECT * FROM config_versions
            WHERE active = 1 ORDER BY config_name
        """)
    result = {
        "database": str(path),
        "account_id": account_id,
        "account": account,
        "positions": _position_frame(positions),
        "orders": _order_frame(orders),
        "fills": _fill_frame(fills),
        "strategies": _strategy_frame(strategies),
        "risk_state": risk_state,
        "risk_events": _risk_frame(risk_events),
        "reconciliation": reconciliation,
        "differences": _difference_frame(differences),
        "commands": _command_frame(commands),
        "heartbeat": heartbeat,
        "active_configs": active_configs,
    }
    result["daily_pnl"] = _daily_pnl(account, cash_snapshots)
    result["runtime_online"] = _heartbeat_online(heartbeat)
    return result


def render_environment_band(environment):
    colors = {
        "paper": ("#ecf8ef", "#177245", "#b7dfc5"),
        "danger": ("#fff0f0", "#b42318", "#f2b8b5"),
        "neutral": ("#f5f5f3", "#52514e", "#d8d8d2"),
    }
    background, color, border = colors[environment["tone"]]
    st.markdown(
        f"""
        <div class="quant-env-band" style="display:flex;align-items:center;gap:14px;
                    border:1px solid {border};background:{background};
                    padding:10px 14px;margin-bottom:12px;">
          <span style="font-size:18px;font-weight:700;color:{color};">
            {html.escape(environment['kind'])}
          </span>
          <span style="color:#52514e;">
            TRADING_STAGE={html.escape(environment['stage'])} ·
            QMT={html.escape(environment['qmt_mode'])} ·
            真实资金={'允许' if environment['allow_real_trading'] else '禁止'}
          </span>
          <span class="quant-env-note" style="margin-left:auto;color:{color};">
            {html.escape(environment['note'])}
          </span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_account_metrics(snapshot, environment):
    account = snapshot["account"] or {}
    pnl = snapshot["daily_pnl"]
    online = snapshot["runtime_online"]
    total_asset = float(account.get("total_asset", 0.0))
    available = float(account.get("available_cash", 0.0))
    market_value = float(account.get("market_value", 0.0))
    positions = snapshot["positions"]
    unrealized = (
        float(positions["浮动盈亏"].sum()) if not positions.empty else 0.0
    )
    primary = st.columns(4)
    primary[0].metric("总资产", _money(total_asset))
    primary[1].metric("可用资金", _money(available))
    primary[2].metric("持仓市值", _money(market_value))
    primary[3].metric("今日盈亏", _money(pnl["amount"]), _pct(pnl["pct"]))
    secondary = st.columns(3)
    secondary[0].metric("持仓浮盈", _money(unrealized))
    secondary[1].metric("运行实例", "在线" if online else "离线")
    secondary[2].metric("风险状态", _risk_label(snapshot["risk_state"]))
    heartbeat = snapshot.get("heartbeat") or {}
    if online:
        st.caption(
            f"运行 ID {heartbeat.get('runtime_id', '—')} · "
            f"状态 {heartbeat.get('status', '—')} · "
            f"最后心跳 {heartbeat.get('last_cycle_at', '—')}"
        )
    else:
        st.warning("交易进程心跳离线，页面数据可能已经过期。")


def _render_control_panel(path, account_id, snapshot):
    st.subheader("运行控制与急停")
    online = snapshot["runtime_online"]
    if not online:
        st.warning("交易进程离线。控制命令会排队，进程恢复后才会执行。")
    operator = st.text_input(
        "操作人",
        value=st.session_state.get(
            "trading_operator", os.getenv("USER", "operator")
        ),
        key="trading_operator",
    )
    c1, c2, c3, c4 = st.columns(4)
    if c1.button("停止开仓", width="stretch"):
        _set_pending_command("stop_open")
    if c2.button("只减仓", width="stretch"):
        _set_pending_command("reduce_only")
    if c3.button("立即对账", width="stretch"):
        _set_pending_command("reconcile")
    if c4.button("全局急停并撤单", type="primary", width="stretch"):
        _set_pending_command("kill_switch")

    pending = st.session_state.get("pending_runtime_command")
    if pending:
        labels = {
            "stop_open": "停止新开仓",
            "reduce_only": "进入只减仓",
            "reconcile": "立即执行账户对账",
            "kill_switch": "全局急停、撤销全部可撤订单并冻结交易",
        }
        st.warning(f"待确认命令：{labels[pending]}")
        confirmed = st.checkbox(
            "我已核对环境、账户和影响，确认执行该命令",
            key=f"confirm_runtime_command_{pending}",
        )
        a, b = st.columns([1, 4])
        if a.button(
            "确认执行", type="primary", disabled=not confirmed,
            width="stretch",
        ):
            if not operator.strip():
                st.error("操作人不能为空。")
            else:
                repository = TradingRepository(
                    SQLiteDatabase(path)
                )
                try:
                    repository.request_runtime_command(
                        account_id=account_id,
                        command_type=pending,
                        requested_by=operator.strip(),
                        payload={"reason": labels[pending], "source": "web_ui"},
                    )
                finally:
                    repository.close()
                st.session_state.pop("pending_runtime_command", None)
                st.success("命令已写入队列，等待交易进程执行。")
                st.rerun()
        if b.button("取消", width="stretch"):
            st.session_state.pop("pending_runtime_command", None)
            st.rerun()

    state = snapshot["risk_state"] or {}
    if state.get("state") == "killed":
        st.error(
            "风险状态为 KILLED。必须完成对账并由人工恢复后才能继续交易。"
        )
    elif state.get("state") == "reduce_only":
        st.warning("风险状态为 REDUCE_ONLY，只允许卖出和风险退出。")
    elif state.get("state") == "stop_open":
        st.warning("风险状态为 STOP_OPEN，禁止新开仓。")


def _render_trading_tables(snapshot):
    tabs = st.tabs([
        "持仓", "订单", "成交", "策略状态", "风险与对账", "运行命令", "运行配置"
    ])
    with tabs[0]:
        _table(snapshot["positions"], "当前没有持仓。")
    with tabs[1]:
        _table(snapshot["orders"], "当前没有订单。")
    with tabs[2]:
        _table(snapshot["fills"], "当前没有成交。")
    with tabs[3]:
        _table(snapshot["strategies"], "当前没有策略实例。")
    with tabs[4]:
        reconciliation = snapshot["reconciliation"]
        if reconciliation:
            c1, c2, c3 = st.columns(3)
            c1.metric("最近对账", reconciliation.get("status", "—"))
            c2.metric("差异数量", reconciliation.get("difference_count", 0))
            c3.metric("交易日期", reconciliation.get("trade_date") or "—")
        _table(snapshot["differences"], "没有对账差异。")
        st.markdown("#### 风险事件")
        _table(snapshot["risk_events"], "没有风险事件。")
    with tabs[5]:
        _table(snapshot["commands"], "没有运行命令。")
    with tabs[6]:
        configs = snapshot["active_configs"]
        if not configs:
            st.info("没有活动配置版本。")
        for item in configs:
            with st.expander(
                f"{item.get('config_name')} · {item.get('version')}"
            ):
                st.json(item.get("payload", {}))
        st.caption(f"数据库：{snapshot['database']}")


def _set_pending_command(command_type):
    st.session_state["pending_runtime_command"] = command_type
    st.rerun()


@contextmanager
def _readonly_connection(path):
    path = Path(path).resolve()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        yield conn
    finally:
        conn.close()


def _table_exists(conn, table):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _all(conn, sql, params=()):
    name = sql.split("FROM", 1)[1].strip().split()[0]
    if not _table_exists(conn, name):
        return []
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _one(conn, sql, params=()):
    name = sql.split("FROM", 1)[1].strip().split()[0]
    if not _table_exists(conn, name):
        return None
    conn.row_factory = sqlite3.Row
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _account_ids(path):
    with _readonly_connection(path) as conn:
        if not _table_exists(conn, "cash_current"):
            return []
        rows = conn.execute("""
            SELECT account_id FROM cash_current ORDER BY updated_at DESC
        """).fetchall()
    return [str(row[0]) for row in rows]


def _fills_for_orders(conn, order_ids):
    if not order_ids or not _table_exists(conn, "fills"):
        return []
    placeholders = ",".join("?" for _ in order_ids)
    return _all(conn, f"""
        SELECT * FROM fills WHERE local_order_id IN ({placeholders})
        ORDER BY filled_at DESC LIMIT 500
    """, tuple(order_ids))


def _position_frame(rows):
    values = []
    for item in rows:
        total = int(item.get("total_quantity", 0))
        cost = float(item.get("average_cost", 0.0))
        price = float(item.get("market_price", 0.0))
        pnl = (price - cost) * total
        values.append({
            "股票代码": item.get("symbol", ""),
            "持仓数量": total,
            "可用数量": int(item.get("available_quantity", 0)),
            "冻结数量": int(item.get("frozen_quantity", 0)),
            "成本价": cost,
            "最新价": price,
            "市值": float(item.get("market_value", 0.0)),
            "浮动盈亏": pnl,
            "盈亏比例": price / cost - 1 if cost else 0.0,
            "更新时间": item.get("updated_at", ""),
        })
    return pd.DataFrame(values)


def _order_frame(rows):
    values = []
    for item in rows:
        values.append({
            "本地订单": item.get("local_order_id", ""),
            "券商订单": item.get("broker_order_id", ""),
            "股票代码": item.get("symbol", ""),
            "方向": item.get("side", ""),
            "状态": item.get("status", ""),
            "委托数量": item.get("requested_quantity", 0),
            "已成交": item.get("filled_quantity", 0),
            "剩余": max(
                0,
                int(item.get("requested_quantity", 0))
                - int(item.get("filled_quantity", 0)),
            ),
            "创建时间": item.get("created_at", ""),
            "更新时间": item.get("updated_at", ""),
        })
    return pd.DataFrame(values)


def _fill_frame(rows):
    values = []
    for item in rows:
        values.append({
            "成交编号": item.get("broker_trade_id", ""),
            "券商订单": item.get("broker_order_id", ""),
            "股票代码": item.get("symbol", ""),
            "方向": item.get("side", ""),
            "成交数量": item.get("filled_quantity", 0),
            "成交价格": item.get("fill_price", 0.0),
            "费用": item.get("total_fee", 0.0),
            "成交时间": item.get("filled_at", ""),
        })
    return pd.DataFrame(values)


def _strategy_frame(rows):
    values = []
    for item in rows:
        payload = _json(item.get("payload_json"), {})
        values.append({
            "策略实例": item.get("strategy_instance_id", ""),
            "策略": item.get("strategy_key", ""),
            "版本": item.get("strategy_version", ""),
            "状态": item.get("status", ""),
            "参数": json.dumps(
                payload.get("parameters", payload),
                ensure_ascii=False,
            ),
            "更新时间": item.get("updated_at", ""),
        })
    return pd.DataFrame(values)


def _risk_frame(rows):
    values = []
    for item in rows:
        payload = _json(item.get("payload_json"), {})
        values.append({
            "时间": item.get("occurred_at", ""),
            "规则": item.get("rule_code", ""),
            "级别": item.get("severity", ""),
            "动作": item.get("action", ""),
            "状态": item.get("status", ""),
            "股票": item.get("symbol") or "",
            "说明": payload.get("message", ""),
        })
    return pd.DataFrame(values)


def _difference_frame(rows):
    values = []
    for item in rows:
        values.append({
            "差异类型": item.get("difference_type", ""),
            "股票": item.get("symbol") or "",
            "本地值": item.get("local_value") or "",
            "券商值": item.get("broker_value") or "",
            "状态": item.get("status", ""),
            "处理人": item.get("resolved_by") or "",
            "创建时间": item.get("created_at", ""),
        })
    return pd.DataFrame(values)


def _command_frame(rows):
    values = []
    for item in rows:
        result = _json(item.get("result_json"), {})
        payload = _json(item.get("payload_json"), {})
        values.append({
            "命令": item.get("command_type", ""),
            "状态": item.get("status", ""),
            "操作人": item.get("requested_by", ""),
            "原因": payload.get("reason", ""),
            "结果": json.dumps(result, ensure_ascii=False),
            "请求时间": item.get("requested_at", ""),
            "完成时间": item.get("completed_at") or "",
        })
    return pd.DataFrame(values)


def _daily_pnl(account, snapshots):
    if not account or not snapshots:
        return {"amount": 0.0, "pct": 0.0, "baseline": 0.0}
    current = float(account.get("total_asset", 0.0))
    today = dt.date.today().isoformat()
    today_values = [
        item for item in snapshots
        if str(item.get("snapshot_time", "")).startswith(today)
    ]
    if today_values:
        baseline = float(today_values[-1].get("total_asset", current))
    else:
        baseline = float(snapshots[-1].get("total_asset", current))
    amount = current - baseline
    pct = amount / baseline if baseline else 0.0
    return {"amount": amount, "pct": pct, "baseline": baseline}


def _heartbeat_online(heartbeat):
    if not heartbeat:
        return False
    try:
        updated = dt.datetime.fromisoformat(heartbeat["last_cycle_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return (dt.datetime.now() - updated).total_seconds() <= 30


def _json(value, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _table(frame, empty_message):
    if frame is None or frame.empty:
        st.info(empty_message)
        return
    st.dataframe(frame, width="stretch", hide_index=True)


def _money(value):
    return f"¥{float(value or 0):,.2f}"


def _pct(value):
    return f"{float(value or 0):.2%}"


def _risk_label(state):
    mapping = {
        "active": "正常",
        "stop_open": "停止开仓",
        "reduce_only": "只减仓",
        "killed": "全局急停",
    }
    return mapping.get((state or {}).get("state"), "未知")
