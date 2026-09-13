"""逐 K 线的现实成交模型。

模型约定：
- 第 t 根 K 线收盘后产生的信号，最早在第 t+1 根 K 线开盘价附近成交。
- 买入先受资金约束，卖出先受 T+1 可用持仓约束。
- 成交量、涨跌停、停牌和订单有效期共同决定是否成交及成交数量。
- 无法一次成交的订单保留为挂单，并在之后 K 线继续撮合。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from utils.config import (
    COMMISSION_MIN,
    STAMP_TAX_RATE,
    TRANSFER_FEE_RATE,
    get_price_limit,
)


EPSILON = 1e-9


@dataclass(frozen=True)
class ExecutionConfig:
    """现实成交模型参数。"""

    init_cash: float = 1_000_000.0
    commission: float = 0.00025
    commission_min: float = COMMISSION_MIN
    slippage: float = 0.001
    impact_coefficient: float = 0.02
    max_slippage: float = 0.05
    participation_rate: float = 0.05
    lot_size: int | None = 100
    t_plus_1: bool = True
    price_limit: bool = True
    stamp_tax: bool = True
    transfer_fee: bool = True
    limit_queue_fill_ratio: float = 0.25
    order_ttl_bars: int = 5
    code: str | None = None

    def __post_init__(self):
        if self.init_cash <= 0:
            raise ValueError("初始资金必须大于 0")
        if not 0 <= self.commission < 1:
            raise ValueError("佣金费率必须在 [0, 1) 之间")
        if self.commission_min < 0:
            raise ValueError("最低佣金不能小于 0")
        if not 0 <= self.slippage < 1:
            raise ValueError("基础滑点必须在 [0, 1) 之间")
        if self.impact_coefficient < 0:
            raise ValueError("冲击成本系数不能小于 0")
        if not 0 <= self.max_slippage < 1:
            raise ValueError("最大滑点必须在 [0, 1) 之间")
        if not 0 < self.participation_rate <= 1:
            raise ValueError("成交量参与率必须在 (0, 1] 之间")
        if self.lot_size is not None and self.lot_size <= 0:
            raise ValueError("交易单位必须大于 0")
        if not 0 <= self.limit_queue_fill_ratio <= 1:
            raise ValueError("涨跌停排队成交比例必须在 [0, 1] 之间")
        if self.order_ttl_bars < 1:
            raise ValueError("订单有效期必须至少为 1 根 K 线")

    @property
    def max_total_slippage(self) -> float:
        return min(
            self.max_slippage,
            self.slippage + self.impact_coefficient,
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExecutionResult:
    """成交模型输出。"""

    portfolio: "SimulatedPortfolio"
    fills: pd.DataFrame
    orders: pd.DataFrame
    executed_entries: pd.Series
    executed_exits: pd.Series
    stats: dict = field(default_factory=dict)


class SimulatedTrades:
    """兼容 RiskAnalyzer 和交易明细导出的交易集合。"""

    def __init__(self, records_readable: pd.DataFrame):
        self.records_readable = records_readable


class SimulatedPortfolio:
    """兼容现有分析和绘图接口的轻量组合对象。"""

    def __init__(self, *, equity, asset_value, position, trades):
        self._equity = equity
        self._asset_value = asset_value
        self._position = position
        self.trades = SimulatedTrades(trades)

    def value(self):
        return self._equity.copy()

    def asset_value(self):
        return self._asset_value.copy()

    def position_mask(self):
        return (self._position > 0).astype(bool)

    def position_shares(self):
        return self._position.copy()


class RealisticExecutionSimulator:
    """逐 K 线撮合策略信号。"""

    def __init__(self, df: pd.DataFrame, entries: pd.Series,
                 exits: pd.Series, config: ExecutionConfig):
        self.df = df
        self.entries = entries.astype(bool)
        self.exits = exits.astype(bool)
        self.config = config

    def run(self) -> ExecutionResult:
        n = len(self.df)
        if n == 0:
            raise ValueError("成交模型行情数据为空")
        if len(self.entries) != n or len(self.exits) != n:
            raise ValueError("策略信号长度与行情数据不一致")

        opens = self.df["open"].to_numpy(dtype=float)
        highs = self.df["high"].to_numpy(dtype=float)
        lows = self.df["low"].to_numpy(dtype=float)
        closes = self.df["close"].to_numpy(dtype=float)
        volumes = self.df["volume"].to_numpy(dtype=float)
        previous_close = self.df["close"].shift(1)
        limit_pct = get_price_limit(self.config.code) \
            if self.config.code else 0.10
        limit_up = (previous_close * (1 + limit_pct)).round(2).to_numpy()
        limit_down = (previous_close * (1 - limit_pct)).round(2).to_numpy()

        cash = self.config.init_cash
        position = 0
        lots: list[dict] = []
        pending = None
        order_seq = 0
        fill_seq = 0
        orders: list[dict] = []
        fills: list[dict] = []
        equity_values = np.zeros(n, dtype=float)
        position_values = np.zeros(n, dtype=int)
        asset_values = np.zeros(n, dtype=float)
        executed_entries = pd.Series(False, index=self.df.index)
        executed_exits = pd.Series(False, index=self.df.index)
        open_trade = None
        closed_trades: list[dict] = []

        for i in range(n):
            # 只使用上一根 K 线收盘后已知的信号，避免未来函数。
            if i > 0:
                exit_signal = bool(self.exits.iloc[i - 1])
                entry_signal = bool(self.entries.iloc[i - 1])
                if exit_signal:
                    if pending is not None and pending["side"] == "buy":
                        _finish_order(pending, "CANCELLED", i, "SELL_SIGNAL")
                        pending = None
                    if position > 0 and (
                        pending is None or pending["side"] != "sell"
                    ):
                        order_seq += 1
                        pending = {
                            "order_id": order_seq,
                            "side": "sell",
                            "created_idx": i,
                            "updated_idx": i,
                            "requested": int(position),
                            "remaining": int(position),
                            "filled": 0,
                            "status": "SUBMITTED",
                            "reason": "",
                            "had_partial": False,
                            "gross_filled": 0.0,
                            "commission_charged": 0.0,
                        }
                        orders.append(pending)
                elif entry_signal and position == 0 and pending is None:
                    order_seq += 1
                    pending = {
                        "order_id": order_seq,
                        "side": "buy",
                        "created_idx": i,
                        "updated_idx": i,
                        "requested": None,
                        "remaining": None,
                        "filled": 0,
                        "status": "SUBMITTED",
                        "reason": "",
                        "had_partial": False,
                        "gross_filled": 0.0,
                        "commission_charged": 0.0,
                    }
                    orders.append(pending)

            if pending is not None:
                age = i - pending["created_idx"]
                if age >= self.config.order_ttl_bars:
                    _finish_order(pending, "CANCELLED", i, "TTL_EXPIRED")
                    pending = None

            if pending is not None:
                fill = self._try_fill(
                    i, pending, cash, position, lots,
                    opens, highs, lows, volumes,
                    limit_up=limit_up, limit_down=limit_down,
                )
                if fill is not None:
                    qty, price, fees, slippage_rate, target_qty = fill
                    if pending["requested"] is None:
                        pending["requested"] = int(target_qty)
                    fill_gross = qty * price
                    if pending["side"] == "buy":
                        cash -= fill_gross + fees["total"]
                        position += qty
                        lots.append({"qty": qty, "buy_idx": i})
                        if open_trade is None:
                            open_trade = _new_trade()
                        _add_entry(open_trade, self.df.index[i], qty, price, fees["total"])
                        executed_entries.iloc[i] = True
                    else:
                        cash += fill_gross - fees["total"]
                        position -= qty
                        _consume_lots(lots, qty)
                        if open_trade is None:
                            open_trade = _new_trade()
                        _add_exit(open_trade, self.df.index[i], qty, price, fees["total"])
                        executed_exits.iloc[i] = True

                    fill_seq += 1
                    fills.append({
                        "成交编号": fill_seq,
                        "订单号": pending["order_id"],
                        "日期": self.df.index[i],
                        "方向": "买入" if pending["side"] == "buy" else "卖出",
                        "数量": int(qty),
                        "价格": float(price),
                        "成交金额": float(qty * price),
                        "佣金": float(fees["commission"]),
                        "印花税": float(fees["stamp_tax"]),
                        "过户费": float(fees["transfer_fee"]),
                        "总费用": float(fees["total"]),
                        "滑点": float(slippage_rate),
                        "成交量参与率": float(
                            qty / max(volumes[i], EPSILON)
                        ),
                    })
                    pending["filled"] += int(qty)
                    pending["gross_filled"] += float(fill_gross)
                    pending["commission_charged"] += float(fees["commission"])
                    pending["updated_idx"] = i
                    pending["remaining"] = max(
                        0, int(pending["requested"] - pending["filled"])
                    )

                    if pending["remaining"] == 0 or position == 0:
                        _finish_order(pending, "FILLED", i, "")
                        if open_trade is not None and position == 0:
                            closed_trades.append(
                                _finalize_trade(open_trade, closes[i], closed=True)
                            )
                            open_trade = None
                        pending = None
                    else:
                        pending["had_partial"] = True
                        _finish_order(pending, "PARTIALLY_FILLED", i, "PARTIAL")

            asset_value = position * closes[i]
            position_values[i] = int(position)
            asset_values[i] = float(asset_value)
            equity_values[i] = float(cash + asset_value)

        if pending is not None:
            _finish_order(pending, "CANCELLED", n - 1, "END_OF_DATA")
        if open_trade is not None:
            closed_trades.append(
                _finalize_trade(open_trade, closes[-1], closed=False)
            )

        equity = pd.Series(equity_values, index=self.df.index, name="equity")
        asset_value = pd.Series(
            asset_values, index=self.df.index, name="asset_value"
        )
        position_series = pd.Series(
            position_values, index=self.df.index, name="position"
        )
        trades = _trades_frame(closed_trades)
        portfolio = SimulatedPortfolio(
            equity=equity,
            asset_value=asset_value,
            position=position_series,
            trades=trades,
        )
        fills_frame = pd.DataFrame(fills, columns=[
            "成交编号", "订单号", "日期", "方向", "数量", "价格",
            "成交金额", "佣金", "印花税", "过户费", "总费用",
            "滑点", "成交量参与率",
        ])
        orders_frame = pd.DataFrame(orders, columns=[
            "order_id", "side", "created_idx", "updated_idx",
            "requested", "remaining", "filled", "status", "reason",
            "had_partial",
            "gross_filled", "commission_charged",
        ])
        if len(orders_frame):
            index_values = self.df.index
            orders_frame["created_at"] = [
                index_values[int(item)] for item in orders_frame["created_idx"]
            ]
            orders_frame["updated_at"] = [
                index_values[int(item)] for item in orders_frame["updated_idx"]
            ]
        stats = {
            "total_fee": float(fills_frame["总费用"].sum()) if len(fills_frame) else 0.0,
            "total_commission": float(fills_frame["佣金"].sum()) if len(fills_frame) else 0.0,
            "total_stamp_tax": float(fills_frame["印花税"].sum()) if len(fills_frame) else 0.0,
            "total_transfer_fee": float(fills_frame["过户费"].sum()) if len(fills_frame) else 0.0,
            "fill_count": int(len(fills_frame)),
            "order_count": int(len(orders_frame)),
            "partial_order_count": int(
                orders_frame["had_partial"].sum()
            ) if len(orders_frame) else 0,
            "filled_quantity": int(fills_frame["数量"].sum()) if len(fills_frame) else 0,
            "average_slippage": float(
                np.average(
                    fills_frame["滑点"],
                    weights=fills_frame["成交金额"],
                )
            ) if len(fills_frame) and fills_frame["成交金额"].sum() > 0 else 0.0,
        }
        return ExecutionResult(
            portfolio=portfolio,
            fills=fills_frame,
            orders=orders_frame,
            executed_entries=executed_entries,
            executed_exits=executed_exits,
            stats=stats,
        )

    def _try_fill(self, i, pending, cash, position, lots,
                  opens, highs, lows, volumes, *, limit_up, limit_down):
        if self._is_suspended(i):
            pending["reason"] = "SUSPENDED"
            return None
        bar_open = float(opens[i])
        bar_high = float(highs[i])
        bar_low = float(lows[i])
        bar_volume = float(volumes[i])
        if bar_volume <= 0 or not np.isfinite(bar_volume):
            pending["reason"] = "SUSPENDED_OR_NO_VOLUME"
            return None

        side = pending["side"]
        if side == "sell":
            available = _available_quantity(lots, i, self.config.t_plus_1)
            if available <= 0:
                pending["reason"] = "T_PLUS_1_LOCKED"
                return None
            capacity = min(int(pending["remaining"]), available)
        else:
            capacity = pending["remaining"]

        queue_ratio = 1.0
        if self.config.price_limit and np.isfinite(limit_up[i]):
            if side == "buy" and bar_open >= limit_up[i] - EPSILON:
                sealed = bar_low >= limit_up[i] - EPSILON
                queue_ratio = 0.0 if sealed else self.config.limit_queue_fill_ratio
                if queue_ratio <= 0:
                    pending["reason"] = "LIMIT_UP_QUEUE"
                    return None
            if side == "sell" and bar_open <= limit_down[i] + EPSILON:
                sealed = bar_high <= limit_down[i] + EPSILON
                queue_ratio = 0.0 if sealed else self.config.limit_queue_fill_ratio
                if queue_ratio <= 0:
                    pending["reason"] = "LIMIT_DOWN_QUEUE"
                    return None

        max_capacity = _participation_capacity(
            bar_volume, self.config.participation_rate, self.config.lot_size
        )
        max_capacity = max(0, int(max_capacity * queue_ratio))
        requested = capacity if capacity is not None else None

        if side == "buy":
            affordable = _affordable_buy_quantity(
                cash,
                bar_open,
                self.config,
                remaining=requested,
                previous_gross=pending["gross_filled"],
                commission_charged=pending["commission_charged"],
            )
            qty = min(value for value in (requested, affordable, max_capacity)
                      if value is not None)
            target_qty = affordable
        else:
            qty = min(requested, max_capacity)
            target_qty = requested
        if self.config.lot_size and side == "buy":
            qty = _round_lot_down(qty, self.config.lot_size)
        elif self.config.lot_size and side == "sell" and qty < requested:
            qty = _round_lot_down(qty, self.config.lot_size)
        if qty <= 0:
            pending["reason"] = "LIQUIDITY_OR_CASH_LIMIT"
            return None

        participation = qty / bar_volume
        impact = min(
            self.config.slippage
            + self.config.impact_coefficient * math.sqrt(max(participation, 0.0)),
            self.config.max_slippage,
        )
        raw_price = bar_open * (1 + impact if side == "buy" else 1 - impact)
        price = min(max(raw_price, bar_low), bar_high)
        if self.config.price_limit and np.isfinite(limit_up[i]):
            price = min(price, limit_up[i])
        if self.config.price_limit and np.isfinite(limit_down[i]):
            price = max(price, limit_down[i])
        if side == "sell" and price <= 0:
            return None

        gross = qty * price
        fees = _calculate_fees(
            side,
            gross,
            self.config,
            previous_gross=pending["gross_filled"],
            commission_charged=pending["commission_charged"],
        )
        if side == "buy":
            while qty > 0 and gross + fees["total"] > cash + EPSILON:
                if self.config.lot_size:
                    qty -= self.config.lot_size
                else:
                    qty = 0
                if qty <= 0:
                    pending["reason"] = "INSUFFICIENT_CASH_AFTER_FEES"
                    return None
                gross = qty * price
                fees = _calculate_fees(
                    side,
                    gross,
                    self.config,
                    previous_gross=pending["gross_filled"],
                    commission_charged=pending["commission_charged"],
                )
        return qty, price, fees, impact, target_qty

    def _is_suspended(self, i) -> bool:
        row = self.df.iloc[i]
        if "suspended" in self.df.columns and bool(row["suspended"]):
            return True
        if "trade_status" in self.df.columns:
            status = str(row["trade_status"]).strip().lower()
            if status in {"suspended", "halted", "停牌", "停牌交易"}:
                return True
        return False


def _participation_capacity(volume, participation_rate, lot_size) -> int:
    raw = max(0, int(volume * participation_rate))
    if lot_size:
        raw = _round_lot_down(raw, lot_size)
    return raw


def _round_lot_down(value, lot_size) -> int:
    if not lot_size:
        return max(0, int(value))
    return max(0, int(value) // int(lot_size) * int(lot_size))


def _affordable_buy_quantity(cash, price, config, *, remaining=None,
                             previous_gross=0.0,
                             commission_charged=0.0) -> int:
    if price <= 0 or cash <= 0:
        return 0
    estimate_rate = config.commission + (
        TRANSFER_FEE_RATE if config.transfer_fee else 0.0
    )
    qty = int(cash / (price * (1 + estimate_rate)))
    if config.lot_size:
        qty = _round_lot_down(qty, config.lot_size)
    if remaining is not None:
        qty = min(qty, int(remaining))
    while qty > 0:
        gross = qty * price
        fees = _calculate_fees(
            "buy",
            gross,
            config,
            previous_gross=previous_gross,
            commission_charged=commission_charged,
        )
        if gross + fees["total"] <= cash + EPSILON:
            return qty
        if config.lot_size:
            qty -= config.lot_size
        else:
            qty -= 1
    return 0


def _calculate_fees(side, gross, config, *, previous_gross=0.0,
                    commission_charged=0.0) -> dict:
    commission = 0.0
    if config.commission > 0 or config.commission_min > 0:
        cumulative_gross = previous_gross + gross
        target_commission = max(
            cumulative_gross * config.commission,
            config.commission_min,
        )
        commission = max(0.0, target_commission - commission_charged)
    transfer_fee = gross * TRANSFER_FEE_RATE if config.transfer_fee else 0.0
    stamp_tax = (
        gross * STAMP_TAX_RATE
        if side == "sell" and config.stamp_tax else 0.0
    )
    return {
        "commission": float(commission),
        "stamp_tax": float(stamp_tax),
        "transfer_fee": float(transfer_fee),
        "total": float(commission + stamp_tax + transfer_fee),
    }


def _available_quantity(lots, current_idx, t_plus_1) -> int:
    if not t_plus_1:
        return sum(item["qty"] for item in lots)
    return sum(item["qty"] for item in lots if item["buy_idx"] < current_idx)


def _consume_lots(lots, quantity):
    remaining = int(quantity)
    while remaining > 0 and lots:
        head = lots[0]
        used = min(head["qty"], remaining)
        head["qty"] -= used
        remaining -= used
        if head["qty"] == 0:
            lots.pop(0)


def _finish_order(order, status, idx, reason):
    order["status"] = status
    order["reason"] = reason
    order["updated_idx"] = idx
    if order["requested"] is None:
        order["requested"] = int(order["filled"])
    order["remaining"] = max(
        0, int(order["requested"] - order["filled"])
    )


def _new_trade() -> dict:
    return {
        "entry_dates": [],
        "entry_quantities": [],
        "entry_prices": [],
        "entry_costs": 0.0,
        "exit_dates": [],
        "exit_quantities": [],
        "exit_prices": [],
        "exit_costs": 0.0,
    }


def _add_entry(trade, date, qty, price, cost):
    trade["entry_dates"].append(date)
    trade["entry_quantities"].append(int(qty))
    trade["entry_prices"].append(float(price))
    trade["entry_costs"] += float(cost)


def _add_exit(trade, date, qty, price, cost):
    trade["exit_dates"].append(date)
    trade["exit_quantities"].append(int(qty))
    trade["exit_prices"].append(float(price))
    trade["exit_costs"] += float(cost)


def _finalize_trade(trade, last_close, *, closed: bool) -> dict:
    entry_qty = sum(trade["entry_quantities"])
    entry_notional = sum(
        qty * price
        for qty, price in zip(
            trade["entry_quantities"], trade["entry_prices"]
        )
    )
    exit_qty = sum(trade["exit_quantities"])
    exit_notional = sum(
        qty * price
        for qty, price in zip(
            trade["exit_quantities"], trade["exit_prices"]
        )
    )
    open_qty = max(0, entry_qty - exit_qty)
    exit_notional += open_qty * last_close
    pnl = (
        exit_notional
        - entry_notional
        - trade["entry_costs"]
        - trade["exit_costs"]
    )
    avg_entry = entry_notional / entry_qty if entry_qty else np.nan
    avg_exit = exit_notional / entry_qty if entry_qty else np.nan
    return {
        "Entry Timestamp": trade["entry_dates"][0],
        "Exit Timestamp": trade["exit_dates"][-1] if trade["exit_dates"] else pd.NaT,
        "Avg Entry Price": avg_entry,
        "Avg Exit Price": avg_exit if closed else np.nan,
        "Size": int(entry_qty),
        "PnL": float(pnl),
        "Return": float(pnl / entry_notional) if entry_notional else 0.0,
        "Status": "Closed" if closed else "Open",
    }


def _trades_frame(trades: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(trades, columns=[
        "Entry Timestamp", "Exit Timestamp", "Avg Entry Price",
        "Avg Exit Price", "Size", "PnL", "Return", "Status",
    ])
