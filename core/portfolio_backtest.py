"""多标的组合回测引擎。"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
import plotly.graph_objects as go

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.execution_model import (
    EPSILON,
    ExecutionConfig,
    available_quantity,
    calculate_fees,
    consume_lots,
    participation_capacity,
    round_lot_down,
)
from core.risk_analysis import RiskAnalyzer
from utils.config import get_price_limit


@dataclass(frozen=True)
class PortfolioConstraints:
    """组合目标权重约束。"""

    max_weight: float = 0.2
    min_weight: float = 0.0
    max_gross_exposure: float = 1.0
    min_cash_weight: float = 0.05
    max_positions: int | None = None

    def __post_init__(self):
        if not 0 < self.max_weight <= 1:
            raise ValueError("单票最大权重必须在 (0, 1] 之间")
        if not 0 <= self.min_weight <= self.max_weight:
            raise ValueError("单票最小权重必须位于 [0, max_weight]")
        if not 0 < self.max_gross_exposure <= 1:
            raise ValueError("总仓位上限必须在 (0, 1] 之间")
        if not 0 <= self.min_cash_weight < 1:
            raise ValueError("最低现金比例必须在 [0, 1) 之间")
        if self.max_positions is not None and self.max_positions < 1:
            raise ValueError("最大持仓数必须为正整数")


class PortfolioBacktestEngine:
    """按目标权重共享资金池执行多标的回测。"""

    def __init__(
            self,
            data,
            target_weights,
            *,
            init_cash: float = 1_000_000,
            rebalance_frequency="M",
            rebalance_threshold: float = 0.0,
            constraints: PortfolioConstraints | None = None,
            commission: float = 0.00025,
            commission_min: float = 5.0,
            slippage: float = 0.001,
            impact_coefficient: float = 0.02,
            max_slippage: float = 0.05,
            participation_rate: float = 0.05,
            lot_size: int | None = 100,
            t_plus_1: bool = True,
            price_limit: bool = True,
            stamp_tax: bool = True,
            transfer_fee: bool = True,
            limit_queue_fill_ratio: float = 0.25,
            order_ttl_bars: int = 5,
            rf: float = 0.0,
    ):
        if init_cash <= 0:
            raise ValueError("初始资金必须大于 0")
        self.data = _prepare_data(data)
        self.symbols = list(self.data)
        if len(self.symbols) < 2:
            raise ValueError("组合回测至少需要 2 个标的")
        self.index = self._union_index()
        self.constraints = constraints or PortfolioConstraints()
        self.rebalance_frequency = rebalance_frequency
        self.rebalance_threshold = float(rebalance_threshold)
        if self.rebalance_threshold < 0:
            raise ValueError("再平衡阈值不能小于 0")
        self.target_weights = constrain_target_weights(
            target_weights,
            self.index,
            self.symbols,
            self.constraints,
        )
        self.execution_config = ExecutionConfig(
            init_cash=float(init_cash),
            commission=float(commission),
            commission_min=float(commission_min),
            slippage=float(slippage),
            impact_coefficient=float(impact_coefficient),
            max_slippage=float(max_slippage),
            participation_rate=float(participation_rate),
            lot_size=lot_size,
            t_plus_1=t_plus_1,
            price_limit=price_limit,
            stamp_tax=stamp_tax,
            transfer_fee=transfer_fee,
            limit_queue_fill_ratio=float(limit_queue_fill_ratio),
            order_ttl_bars=int(order_ttl_bars),
        )
        self.rf = float(rf)
        self.result = None
        self.experiment_id = None
        self.experiment_record = None

    @classmethod
    def from_signals(
            cls,
            data,
            signals,
            *,
            rebalance_frequency="M",
            constraints: PortfolioConstraints | None = None,
            **kwargs,
    ) -> "PortfolioBacktestEngine":
        """由多标的策略信号构建等权目标权重。"""
        constraints = constraints or PortfolioConstraints()
        index = _union_index_from_data(data)
        symbols = list(data)
        target_weights = signals_to_target_weights(
            signals,
            index,
            symbols,
            allocation="equal_weight",
            constraints=constraints,
        )
        return cls(
            data,
            target_weights,
            rebalance_frequency=rebalance_frequency,
            constraints=constraints,
            **kwargs,
        )

    def run(self, *, record_experiment: bool = True,
            experiment_name: str | None = None,
            experiment_store=None) -> "PortfolioBacktestEngine":
        self.result = _PortfolioSimulation(
            self.data,
            self.index,
            self.symbols,
            self.target_weights,
            self.execution_config,
            self.rebalance_frequency,
            self.rebalance_threshold,
            self.constraints.min_cash_weight,
            self.rf,
        ).run()
        if record_experiment:
            from research.experiments import record_portfolio_experiment

            self.experiment_record = record_portfolio_experiment(
                self.data,
                self.target_weights,
                self.result,
                execution_config=self.execution_config,
                constraints=self.constraints,
                rebalance_frequency=self.rebalance_frequency,
                rebalance_threshold=self.rebalance_threshold,
                rf=self.rf,
                name=experiment_name,
                store=experiment_store,
            )
            self.experiment_id = self.experiment_record.experiment_id
        return self

    def get_metrics(self) -> dict:
        self._ensure_run()
        return dict(self.result.metrics)

    def get_equity(self) -> pd.Series:
        self._ensure_run()
        return self.result.equity.copy()

    def get_positions(self) -> pd.DataFrame:
        self._ensure_run()
        return self.result.position_values.copy()

    def get_weights(self) -> pd.DataFrame:
        self._ensure_run()
        return self.result.weights.copy()

    def get_target_weights(self) -> pd.DataFrame:
        self._ensure_run()
        return self.result.target_weights.copy()

    def get_cash(self) -> pd.Series:
        self._ensure_run()
        return self.result.cash.copy()

    def get_fills(self) -> pd.DataFrame:
        self._ensure_run()
        return self.result.fills.copy()

    def get_orders(self) -> pd.DataFrame:
        self._ensure_run()
        return self.result.orders.copy()

    def get_risk_metrics(self) -> dict:
        self._ensure_run()
        return dict(self.result.risk_metrics)

    def get_pnl_contribution(self) -> pd.DataFrame:
        self._ensure_run()
        return self.result.pnl_contribution.copy()

    def get_correlation_matrix(self) -> pd.DataFrame:
        self._ensure_run()
        return self.result.correlation.copy()

    def get_trades(self) -> pd.DataFrame:
        self._ensure_run()
        return self.result.trades.copy()

    def plot(self) -> dict:
        self._ensure_run()
        return {
            "equity": _plot_equity(self.result),
            "exposure": _plot_exposure(self.result),
        }

    def _ensure_run(self):
        if self.result is None:
            raise RuntimeError("请先调用 run() 执行组合回测")

    def _union_index(self):
        return _union_index_from_data(self.data)


@dataclass
class _PortfolioResult:
    equity: pd.Series
    cash: pd.Series
    position_values: pd.DataFrame
    weights: pd.DataFrame
    target_weights: pd.DataFrame
    gross_exposure: pd.Series
    net_exposure: pd.Series
    hhi: pd.Series
    turnover: pd.Series
    fills: pd.DataFrame
    orders: pd.DataFrame
    trades: pd.DataFrame
    pnl_contribution: pd.DataFrame
    correlation: pd.DataFrame
    metrics: dict
    risk_metrics: dict


class _PortfolioSimulation:
    def __init__(self, data, index, symbols, target_weights, config,
                 rebalance_frequency, rebalance_threshold, min_cash_weight,
                 rf):
        self.data = data
        self.index = index
        self.symbols = symbols
        self.target_weights = target_weights
        self.config = config
        self.rebalance_frequency = rebalance_frequency
        self.rebalance_threshold = rebalance_threshold
        self._min_cash_weight = float(min_cash_weight)
        self.rf = float(rf)

    def run(self) -> _PortfolioResult:
        panels = _build_panels(
            self.data, self.index, self.symbols, self.config
        )
        opens = panels["open"]
        highs = panels["high"]
        lows = panels["low"]
        closes = panels["close"]
        volumes = panels["volume"]
        tradable = panels["tradable"]
        limit_up = panels["limit_up"]
        limit_down = panels["limit_down"]

        cash = float(self.config.init_cash)
        positions = {symbol: 0 for symbol in self.symbols}
        lots = {symbol: [] for symbol in self.symbols}
        cash_flows = {symbol: 0.0 for symbol in self.symbols}
        pending = {}
        orders = []
        fills = []
        order_seq = 0
        fill_seq = 0
        scheduled = _rebalance_schedule(
            self.index, self.rebalance_frequency
        )
        changed = self.target_weights.diff().abs().fillna(0).sum(axis=1) > EPSILON
        equity_values = np.zeros(len(self.index))
        cash_values = np.zeros(len(self.index))
        position_values = np.zeros((len(self.index), len(self.symbols)))
        turnover_values = np.zeros(len(self.index))

        for i, date in enumerate(self.index):
            force_rebalance = bool(scheduled[i] or changed.iloc[i])
            if i > 0 and force_rebalance:
                _cancel_pending(pending, orders, i, "REBALANCE_REPLACED")
                desired = self.target_weights.iloc[i - 1]
                portfolio_open_value = _portfolio_value(
                    cash, positions, opens.iloc[i]
                )
                should_trade = self._should_rebalance(
                    positions,
                    desired,
                    portfolio_open_value,
                    opens.iloc[i],
                    force=bool(changed.iloc[i]),
                )
                if should_trade:
                    new_orders = self._make_rebalance_orders(
                        i,
                        desired,
                        positions,
                        portfolio_open_value,
                        opens.iloc[i],
                        order_seq,
                    )
                    for order in new_orders:
                        order_seq += 1
                        order["order_id"] = order_seq
                        orders.append(order)
                        pending[order["symbol"]] = order

            day_turnover = 0.0
            if pending:
                # 先卖后买，卖出资金当天可用于买入。
                for side in ("sell", "buy"):
                    for symbol in list(pending):
                        order = pending.get(symbol)
                        if order is None or order["side"] != side:
                            continue
                        if i - order["created_idx"] >= self.config.order_ttl_bars:
                            _finish_order(order, "CANCELLED", i, "TTL_EXPIRED")
                            del pending[symbol]
                            continue
                        fill = self._try_fill(
                            i, symbol, order, cash, positions, lots,
                            opens, highs, lows, volumes, tradable,
                            limit_up, limit_down,
                        )
                        if fill is None:
                            continue
                        qty, price, fees, slippage_rate, target_qty = fill
                        if order["requested"] is None:
                            order["requested"] = int(target_qty)
                        gross = qty * price
                        if side == "buy":
                            cash -= gross + fees["total"]
                            positions[symbol] += qty
                            lots[symbol].append({"qty": qty, "buy_idx": i})
                            cash_flows[symbol] -= gross + fees["total"]
                        else:
                            cash += gross - fees["total"]
                            positions[symbol] -= qty
                            consume_lots(lots[symbol], qty)
                            cash_flows[symbol] += gross - fees["total"]
                        day_turnover += gross
                        fill_seq += 1
                        fills.append({
                            "成交编号": fill_seq,
                            "订单号": order["order_id"],
                            "股票代码": symbol,
                            "日期": date,
                            "方向": "买入" if side == "buy" else "卖出",
                            "数量": int(qty),
                            "价格": float(price),
                            "成交金额": float(gross),
                            "佣金": float(fees["commission"]),
                            "印花税": float(fees["stamp_tax"]),
                            "过户费": float(fees["transfer_fee"]),
                            "总费用": float(fees["total"]),
                            "滑点": float(slippage_rate),
                        })
                        order["filled"] += int(qty)
                        order["gross_filled"] += float(gross)
                        order["commission_charged"] += float(fees["commission"])
                        order["remaining"] = max(
                            0, int(order["requested"] - order["filled"])
                        )
                        order["updated_idx"] = i
                        if order["remaining"] == 0:
                            _finish_order(order, "FILLED", i, "")
                            del pending[symbol]
                        else:
                            order["had_partial"] = True
                            _finish_order(
                                order, "PARTIALLY_FILLED", i, "PARTIAL"
                            )

            close_row = closes.iloc[i]
            values = {
                symbol: positions[symbol] * float(close_row[symbol])
                if np.isfinite(close_row[symbol]) else 0.0
                for symbol in self.symbols
            }
            equity = cash + sum(values.values())
            equity_values[i] = equity
            cash_values[i] = cash
            position_values[i, :] = [values[symbol] for symbol in self.symbols]
            turnover_values[i] = day_turnover / equity if equity > 0 else 0.0

        _cancel_pending(pending, orders, len(self.index) - 1, "END_OF_DATA")
        equity = pd.Series(equity_values, index=self.index, name="equity")
        cash_series = pd.Series(
            cash_values, index=self.index, name="cash"
        )
        values_frame = pd.DataFrame(
            position_values, index=self.index, columns=self.symbols
        )
        weights = values_frame.div(
            equity.replace(0, np.nan), axis=0
        ).fillna(0.0)
        gross = weights.sum(axis=1)
        hhi = (weights ** 2).sum(axis=1)
        turnover = pd.Series(
            turnover_values, index=self.index, name="turnover"
        )
        fills_frame = pd.DataFrame(fills, columns=[
            "成交编号", "订单号", "股票代码", "日期", "方向", "数量",
            "价格", "成交金额", "佣金", "印花税", "过户费", "总费用",
            "滑点",
        ])
        orders_frame = pd.DataFrame(orders, columns=[
            "order_id", "symbol", "side", "created_idx", "updated_idx",
            "requested", "remaining", "filled", "status", "reason",
            "had_partial", "gross_filled", "commission_charged",
        ])
        if len(orders_frame):
            orders_frame["created_at"] = [
                self.index[int(item)] for item in orders_frame["created_idx"]
            ]
            orders_frame["updated_at"] = [
                self.index[int(item)] for item in orders_frame["updated_idx"]
            ]

        pnl_rows = []
        final_close = closes.iloc[-1]
        for symbol in self.symbols:
            final_value = (
                positions[symbol] * float(final_close[symbol])
                if np.isfinite(final_close[symbol]) else 0.0
            )
            pnl = cash_flows[symbol] + final_value
            pnl_rows.append({
                "股票代码": symbol,
                "净现金流": float(cash_flows[symbol]),
                "期末市值": float(final_value),
                "组合盈亏（元）": float(pnl),
                "初始资金贡献率": float(pnl / self.config.init_cash),
                "成交笔数": int(
                    (fills_frame["股票代码"] == symbol).sum()
                    if len(fills_frame) else 0
                ),
            })
        pnl_contribution = pd.DataFrame(pnl_rows)
        trades_frame = _build_trade_records(
            fills_frame,
            positions,
            final_close,
            self.symbols,
        )

        closed_pnls = trades_frame.loc[
            trades_frame["Status"] == "Closed", "PnL"
        ].tolist() if len(trades_frame) else []
        analyzer = RiskAnalyzer(
            equity=equity,
            trades=closed_pnls,
            position_value=values_frame.sum(axis=1),
            rf=self.rf,
        )
        risk_metrics = analyzer.compute_metrics()
        returns = equity.pct_change().dropna()
        asset_returns = closes.pct_change().dropna(how="all")
        correlation = asset_returns.corr().fillna(0.0)
        pair_values = correlation.to_numpy()[
            np.triu_indices(len(self.symbols), k=1)
        ] if len(self.symbols) > 1 else np.array([])
        average_correlation = (
            float(np.nanmean(pair_values)) if len(pair_values) else 0.0
        )
        var_95 = float(returns.quantile(0.05)) if len(returns) else 0.0
        cvar_95 = (
            float(returns[returns <= var_95].mean())
            if len(returns) and (returns <= var_95).any() else 0.0
        )
        metrics = {
            **risk_metrics,
            "期末总资产": float(equity.iloc[-1]),
            "总手续费": float(
                fills_frame["总费用"].sum() if len(fills_frame) else 0.0
            ),
            "成交笔数": int(len(fills_frame)),
            "订单数": int(len(orders_frame)),
            "部分成交订单数": int(
                orders_frame["had_partial"].sum() if len(orders_frame) else 0
            ),
            "累计换手率": float(turnover.sum()),
            "平均现金比例": float(
                (cash_series / equity.replace(0, np.nan)).fillna(0).mean()
            ),
            "最大总仓位": float(gross.max()) if len(gross) else 0.0,
            "最大单票权重": float(weights.max().max()) if len(weights) else 0.0,
            "平均持仓集中度HHI": float(hhi.mean()) if len(hhi) else 0.0,
            "平均资产相关性": average_correlation,
            "日VaR95": var_95,
            "日CVaR95": cvar_95,
        }
        return _PortfolioResult(
            equity=equity,
            cash=cash_series,
            position_values=values_frame,
            weights=weights,
            target_weights=self.target_weights.copy(),
            gross_exposure=gross,
            net_exposure=gross.copy(),
            hhi=hhi,
            turnover=turnover,
            fills=fills_frame,
            orders=orders_frame,
            trades=trades_frame,
            pnl_contribution=pnl_contribution,
            correlation=correlation,
            metrics=metrics,
            risk_metrics=risk_metrics,
        )

    def _should_rebalance(self, positions, desired, portfolio_value,
                          open_row, *, force):
        if force:
            return True
        if portfolio_value <= 0:
            return False
        actual = {}
        for symbol in self.symbols:
            price = _safe_price(open_row[symbol])
            value = positions[symbol] * price if price is not None else 0.0
            actual[symbol] = value / portfolio_value
        drift = max(
            abs(float(desired[symbol]) - actual.get(symbol, 0.0))
            for symbol in self.symbols
        )
        return drift > self.rebalance_threshold

    def _make_rebalance_orders(self, i, desired, positions,
                               portfolio_value, open_row, next_order_id):
        orders = []
        deltas = []
        for symbol in self.symbols:
            price = _safe_price(open_row[symbol])
            if price is None:
                continue
            target_qty = round_lot_down(
                desired[symbol] * portfolio_value / price,
                self.config.lot_size,
            )
            delta = target_qty - positions[symbol]
            if delta:
                deltas.append((symbol, delta))

        for symbol, delta in sorted(deltas):
            if delta < 0 and positions[symbol] <= 0:
                continue
            order = {
                "order_id": None,
                "symbol": symbol,
                "side": "sell" if delta < 0 else "buy",
                "created_idx": i,
                "updated_idx": i,
                "requested": int(abs(delta)),
                "remaining": int(abs(delta)),
                "filled": 0,
                "status": "SUBMITTED",
                "reason": "",
                "had_partial": False,
                "gross_filled": 0.0,
                "commission_charged": 0.0,
            }
            orders.append(order)
        return orders

    def _try_fill(self, i, symbol, order, cash, positions, lots,
                  opens, highs, lows, volumes, tradable,
                  limit_up, limit_down):
        if not bool(tradable.iloc[i][symbol]):
            order["reason"] = "SUSPENDED"
            return None
        bar_open = _safe_price(opens.iloc[i][symbol])
        bar_high = _safe_price(highs.iloc[i][symbol])
        bar_low = _safe_price(lows.iloc[i][symbol])
        if None in (bar_open, bar_high, bar_low):
            order["reason"] = "NO_PRICE"
            return None
        volume = float(volumes.iloc[i][symbol])
        if not np.isfinite(volume) or volume <= 0:
            order["reason"] = "SUSPENDED"
            return None

        side = order["side"]
        if side == "sell":
            available = available_quantity(
                lots[symbol], i, self.config.t_plus_1
            )
            if available <= 0:
                order["reason"] = "T_PLUS_1_LOCKED"
                return None
            capacity = min(order["remaining"], available)
        else:
            capacity = order["remaining"]

        queue_ratio = 1.0
        upper = limit_up.iloc[i][symbol]
        lower = limit_down.iloc[i][symbol]
        if side == "buy" and np.isfinite(upper) and bar_open >= upper - EPSILON:
            sealed = bar_low >= upper - EPSILON
            queue_ratio = 0.0 if sealed else self.config.limit_queue_fill_ratio
            if queue_ratio <= 0:
                order["reason"] = "LIMIT_UP_QUEUE"
                return None
        if side == "sell" and np.isfinite(lower) and bar_open <= lower + EPSILON:
            sealed = bar_high <= lower + EPSILON
            queue_ratio = 0.0 if sealed else self.config.limit_queue_fill_ratio
            if queue_ratio <= 0:
                order["reason"] = "LIMIT_DOWN_QUEUE"
                return None

        max_capacity = participation_capacity(
            volume, self.config.participation_rate, self.config.lot_size
        )
        max_capacity = max(0, int(max_capacity * queue_ratio))
        requested = int(capacity)
        if side == "buy":
            portfolio_value = _portfolio_value(
                cash, positions, opens.iloc[i]
            )
            reserve = portfolio_value * self._constraints_min_cash_weight
            available_cash = max(0.0, cash - reserve)
            qty = self._affordable_quantity(
                available_cash, bar_open, requested, order
            )
            qty = min(qty, max_capacity)
        else:
            qty = min(requested, max_capacity)
        if self.config.lot_size and side == "buy":
            qty = round_lot_down(qty, self.config.lot_size)
        elif self.config.lot_size and side == "sell" and qty < requested:
            qty = round_lot_down(qty, self.config.lot_size)
        if qty <= 0:
            order["reason"] = "LIQUIDITY_OR_CASH_LIMIT"
            return None

        participation = qty / volume
        impact = min(
            self.config.slippage
            + self.config.impact_coefficient
            * math.sqrt(max(participation, 0.0)),
            self.config.max_slippage,
        )
        raw_price = bar_open * (1 + impact if side == "buy" else 1 - impact)
        price = min(max(raw_price, bar_low), bar_high)
        if np.isfinite(upper):
            price = min(price, float(upper))
        if np.isfinite(lower):
            price = max(price, float(lower))
        gross = qty * price
        fees = calculate_fees(
            side,
            gross,
            self.config,
            previous_gross=order["gross_filled"],
            commission_charged=order["commission_charged"],
        )
        if side == "buy":
            while qty > 0 and gross + fees["total"] > available_cash + EPSILON:
                qty -= self.config.lot_size or 1
                if qty <= 0:
                    order["reason"] = "INSUFFICIENT_CASH"
                    return None
                gross = qty * price
                fees = calculate_fees(
                    side,
                    gross,
                    self.config,
                    previous_gross=order["gross_filled"],
                    commission_charged=order["commission_charged"],
                )
        return qty, price, fees, impact, requested

    @property
    def _constraints_min_cash_weight(self):
        # 该值在构造组合引擎时写入 ExecutionConfig 之外，组合引擎运行时动态绑定。
        return getattr(self, "_min_cash_weight", 0.0)

    def _affordable_quantity(self, cash, price, requested, order):
        if cash <= 0:
            return 0
        qty = int(cash / max(price, EPSILON))
        qty = round_lot_down(qty, self.config.lot_size)
        qty = min(qty, requested)
        while qty > 0:
            gross = qty * price
            fees = calculate_fees(
                "buy",
                gross,
                self.config,
                previous_gross=order["gross_filled"],
                commission_charged=order["commission_charged"],
            )
            if gross + fees["total"] <= cash + EPSILON:
                return qty
            qty -= self.config.lot_size or 1
        return 0


def signals_to_target_weights(
        signals,
        index,
        symbols,
        *,
        allocation="equal_weight",
        constraints: PortfolioConstraints | None = None,
) -> pd.DataFrame:
    """把多标的买卖信号转换为组合目标权重。"""
    if allocation != "equal_weight":
        raise ValueError("当前只支持 equal_weight 信号分配")
    constraints = constraints or PortfolioConstraints()
    active = pd.DataFrame(False, index=index, columns=symbols)
    for symbol in symbols:
        signal = signals[symbol]
        if hasattr(signal, "entries") and hasattr(signal, "exits"):
            entries, exits = signal.entries, signal.exits
        elif isinstance(signal, tuple) and len(signal) == 2:
            entries, exits = signal
        else:
            raise ValueError(f"{symbol} 的信号格式不正确")
        entries = pd.Series(entries)
        exits = pd.Series(exits)
        if len(entries) == len(index):
            entries.index = index
        else:
            entries = entries.reindex(index)
        if len(exits) == len(index):
            exits.index = index
        else:
            exits = exits.reindex(index)
        entries = entries.fillna(False).astype(bool)
        exits = exits.fillna(False).astype(bool)
        state = False
        values = []
        for entry, exit_ in zip(entries, exits):
            if exit_:
                state = False
            elif entry:
                state = True
            values.append(state)
        active[symbol] = values

    investable = max(0.0, min(
        1.0 - constraints.min_cash_weight,
        constraints.max_gross_exposure,
    ))
    counts = active.sum(axis=1).replace(0, np.nan)
    weights = active.astype(float).div(counts, axis=0) * investable
    return constrain_target_weights(
        weights.fillna(0.0),
        index,
        symbols,
        constraints,
    )


def constrain_target_weights(target_weights, index, symbols,
                             constraints: PortfolioConstraints) -> pd.DataFrame:
    """约束目标权重并保留现金。"""
    if isinstance(target_weights, dict):
        target_weights = pd.DataFrame(target_weights)
    frame = pd.DataFrame(target_weights).copy()
    missing = [symbol for symbol in symbols if symbol not in frame.columns]
    if missing:
        raise ValueError(f"目标权重缺少标的：{missing}")
    if len(frame) == len(index) and not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = index
    frame = frame.reindex(index).ffill().fillna(0.0)[symbols]
    if (frame < -EPSILON).any().any():
        raise ValueError("组合回测不支持负权重和做空")
    if constraints.max_positions is not None:
        max_positions = constraints.max_positions
        for date in frame.index:
            row = frame.loc[date].nlargest(max_positions)
            frame.loc[date, :] = 0.0
            frame.loc[date, row.index] = row.values
    frame = frame.clip(lower=0.0, upper=constraints.max_weight)
    frame = frame.where(frame >= constraints.min_weight, 0.0)
    gross_limit = min(
        constraints.max_gross_exposure,
        1.0 - constraints.min_cash_weight,
    )
    gross = frame.sum(axis=1)
    scale = gross / gross_limit
    scale = scale.where(scale > 1.0, 1.0)
    return frame.div(scale, axis=0).fillna(0.0)


def run_portfolio_backtest(
        data,
        target_weights,
        *,
        rebalance_frequency="M",
        constraints: PortfolioConstraints | None = None,
        **kwargs,
) -> PortfolioBacktestEngine:
    """组合回测快捷入口。"""
    engine = PortfolioBacktestEngine(
        data,
        target_weights,
        rebalance_frequency=rebalance_frequency,
        constraints=constraints,
        **kwargs,
    ).run()
    return engine


def _prepare_data(data) -> dict:
    if not isinstance(data, dict) or len(data) < 2:
        raise ValueError("组合行情需要包含至少 2 个标的的字典")
    prepared = {}
    required = ["open", "high", "low", "close", "volume"]
    for symbol, frame in data.items():
        current = frame.copy()
        if "date" in current.columns:
            current["date"] = pd.to_datetime(current["date"])
            current = current.set_index("date")
        if not isinstance(current.index, pd.DatetimeIndex):
            raise ValueError(f"{symbol} 行情缺少 DatetimeIndex")
        missing = [column for column in required if column not in current.columns]
        if missing:
            raise ValueError(f"{symbol} 行情缺少列：{missing}")
        current = current.sort_index()
        current = current[~current.index.duplicated(keep="last")]
        optional = [
            column for column in ("suspended", "trade_status")
            if column in current.columns
        ]
        prepared[str(symbol)] = current[required + optional].copy()
    return prepared


def _union_index_from_data(data) -> pd.DatetimeIndex:
    values = []
    for frame in data.values():
        current = frame.copy()
        if "date" in current.columns:
            current.index = pd.to_datetime(current["date"])
        values.append(pd.DatetimeIndex(current.index))
    return pd.DatetimeIndex(sorted(set().union(*map(set, values))))


def _build_panels(data, index, symbols, config):
    panels = {}
    for column in ("open", "high", "low", "close"):
        panels[column] = pd.DataFrame({
            symbol: data[symbol][column].reindex(index).ffill()
            for symbol in symbols
        }, index=index)
    panels["volume"] = pd.DataFrame({
        symbol: data[symbol]["volume"].reindex(index).fillna(0.0)
        for symbol in symbols
    }, index=index)
    panels["tradable"] = pd.DataFrame({
        symbol: (
            data[symbol]["volume"].reindex(index).fillna(0.0) > 0
        )
        for symbol in symbols
    }, index=index)
    for column in ("suspended", "trade_status"):
        if any(column in data[symbol].columns for symbol in symbols):
            values = {}
            for symbol in symbols:
                if column in data[symbol].columns:
                    values[symbol] = data[symbol][column].reindex(index)
                else:
                    values[symbol] = False
            extra = pd.DataFrame(values, index=index)
            if column == "suspended":
                panels["tradable"] &= ~extra.fillna(False).astype(bool)
            else:
                blocked = extra.fillna("").astype(str).isin(
                    {"suspended", "halted", "停牌"}
                )
                panels["tradable"] &= ~blocked
    limit_up = {}
    limit_down = {}
    for symbol in symbols:
        pct = get_price_limit(symbol)
        previous = panels["close"][symbol].shift(1)
        limit_up[symbol] = (previous * (1 + pct)).round(2)
        limit_down[symbol] = (previous * (1 - pct)).round(2)
    panels["limit_up"] = pd.DataFrame(limit_up, index=index)
    panels["limit_down"] = pd.DataFrame(limit_down, index=index)
    return panels


def _rebalance_schedule(index: pd.DatetimeIndex, frequency) -> np.ndarray:
    result = np.zeros(len(index), dtype=bool)
    if isinstance(frequency, int):
        if frequency < 1:
            raise ValueError("再平衡周期必须为正整数")
        result[1::frequency] = True
        return result
    value = str(frequency).strip().lower()
    if value in {"d", "daily", "日", "每日"}:
        result[1:] = True
    elif value in {"w", "weekly", "周", "每周"}:
        keys = [(item.isocalendar().year, item.isocalendar().week) for item in index]
    elif value in {"m", "monthly", "月", "每月"}:
        keys = [(item.year, item.month) for item in index]
    elif value in {"q", "quarterly", "季", "每季"}:
        keys = [(item.year, (item.month - 1) // 3 + 1) for item in index]
    else:
        raise ValueError(
            f"不支持的再平衡频率：{frequency!r}，可选 daily/weekly/monthly/quarterly 或整数"
        )
    if value not in {"d", "daily", "日", "每日"}:
        previous = None
        for i, key in enumerate(keys):
            if previous is not None and key != previous:
                result[i] = True
            previous = key
        if len(result):
            result[0] = False
    return result


def _portfolio_value(cash, positions, price_row) -> float:
    value = float(cash)
    for symbol, quantity in positions.items():
        price = _safe_price(price_row[symbol])
        if price is not None:
            value += quantity * price
    return value


def _safe_price(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if np.isfinite(value) and value > 0 else None


def _cancel_pending(pending, orders, idx, reason):
    for symbol in list(pending):
        order = pending.pop(symbol)
        _finish_order(order, "CANCELLED", idx, reason)


def _finish_order(order, status, idx, reason):
    order["status"] = status
    order["reason"] = reason
    order["updated_idx"] = idx
    if order["requested"] is None:
        order["requested"] = int(order["filled"])
    order["remaining"] = max(
        0, int(order["requested"] - order["filled"])
    )


def _build_trade_records(fills, positions, final_close, symbols):
    rows = []
    for symbol in symbols:
        symbol_fills = (
            fills[fills["股票代码"] == symbol].sort_values("日期")
            if len(fills) else pd.DataFrame()
        )
        if symbol_fills.empty:
            continue
        entry_qty = 0
        entry_notional = 0.0
        entry_costs = 0.0
        exit_qty = 0
        exit_notional = 0.0
        exit_costs = 0.0
        entry_date = None
        exit_date = None

        for _, fill in symbol_fills.iterrows():
            qty = int(fill["数量"])
            gross = float(fill["成交金额"])
            fee = float(fill["总费用"])
            if fill["方向"] == "买入":
                if entry_qty == 0:
                    entry_date = fill["日期"]
                entry_qty += qty
                entry_notional += gross
                entry_costs += fee
            else:
                exit_qty += qty
                exit_notional += gross
                exit_costs += fee
                exit_date = fill["日期"]
                if exit_qty >= entry_qty:
                    rows.append(_trade_record(
                        symbol, entry_date, exit_date, entry_qty,
                        entry_notional, entry_costs, exit_qty,
                        exit_notional, exit_costs, 0.0, "Closed",
                    ))
                    entry_qty = exit_qty = 0
                    entry_notional = exit_notional = 0.0
                    entry_costs = exit_costs = 0.0
                    entry_date = exit_date = None

        remaining = max(0, entry_qty - exit_qty)
        if remaining > 0 or positions.get(symbol, 0) > 0:
            last_price = _safe_price(final_close[symbol]) or 0.0
            rows.append(_trade_record(
                symbol, entry_date, exit_date, entry_qty,
                entry_notional, entry_costs, exit_qty,
                exit_notional, exit_costs, remaining * last_price, "Open",
            ))
    return pd.DataFrame(rows, columns=[
        "股票代码", "Entry Timestamp", "Exit Timestamp",
        "Size", "Avg Entry Price", "Avg Exit Price",
        "PnL", "Return", "Status",
    ])


def _trade_record(symbol, entry_date, exit_date, entry_qty,
                  entry_notional, entry_costs, exit_qty,
                  exit_notional, exit_costs, open_value, status):
    pnl = exit_notional + open_value - entry_notional - entry_costs - exit_costs
    avg_entry = entry_notional / entry_qty if entry_qty else np.nan
    avg_exit = exit_notional / exit_qty if exit_qty else np.nan
    return {
        "股票代码": symbol,
        "Entry Timestamp": entry_date,
        "Exit Timestamp": exit_date if status == "Closed" else pd.NaT,
        "Size": int(entry_qty),
        "Avg Entry Price": float(avg_entry),
        "Avg Exit Price": float(avg_exit) if status == "Closed" else np.nan,
        "PnL": float(pnl),
        "Return": float(pnl / entry_notional) if entry_notional else 0.0,
        "Status": status,
    }


def _plot_equity(result):
    figure = go.Figure()
    figure.add_trace(go.Scatter(
        x=result.equity.index,
        y=result.equity.values,
        mode="lines",
        name="组合净值",
    ))
    figure.update_layout(
        title="组合净值",
        xaxis_title="日期",
        yaxis_title="账户资产（元）",
    )
    return figure


def _plot_exposure(result):
    figure = go.Figure()
    figure.add_trace(go.Scatter(
        x=result.gross_exposure.index,
        y=result.gross_exposure.values,
        mode="lines",
        name="总仓位",
        stackgroup="one",
    ))
    figure.add_trace(go.Scatter(
        x=result.hhi.index,
        y=result.hhi.values,
        mode="lines",
        name="持仓集中度 HHI",
        yaxis="y2",
    ))
    figure.update_layout(
        title="组合仓位与集中度",
        xaxis_title="日期",
        yaxis=dict(title="总仓位", tickformat=".0%"),
        yaxis2=dict(title="HHI", overlaying="y", side="right"),
    )
    return figure


def run_test():
    """多标的等权组合回测冒烟测试。"""
    index = pd.date_range("2024-01-01", periods=120, freq="D")
    data = {}
    signals = {}
    for offset, symbol in enumerate(("AAA", "BBB", "CCC")):
        close = pd.Series(
            10 + offset + np.linspace(0, 3 + offset, len(index)),
            index=index,
        )
        data[symbol] = pd.DataFrame({
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": 2_000_000,
        }, index=index)
        entries = pd.Series(False, index=index)
        exits = pd.Series(False, index=index)
        entries.iloc[1 + offset] = True
        signals[symbol] = (entries, exits)
    engine = PortfolioBacktestEngine.from_signals(
        data,
        signals,
        rebalance_frequency="M",
        constraints=PortfolioConstraints(
            max_weight=0.4,
            min_cash_weight=0.1,
            max_positions=3,
        ),
        commission=0.00025,
        slippage=0,
        impact_coefficient=0,
        participation_rate=1.0,
        t_plus_1=False,
        price_limit=False,
    ).run()
    metrics = engine.get_metrics()
    assert metrics["期末总资产"] > 0
    assert 0 <= engine.target_weights.max().max() <= 0.4
    assert metrics["最大单票权重"] > 0
    assert len(engine.get_weights()) == len(index)
    assert len(engine.get_fills()) > 0
    print("===== portfolio backtest tests passed =====")


if __name__ == "__main__":
    run_test()
