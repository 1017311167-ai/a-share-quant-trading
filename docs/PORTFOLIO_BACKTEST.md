# 组合回测

## 1. 目标

组合回测在共享资金池中同时管理多个标的，支持：

- 显式目标权重。
- 多策略信号转换为等权目标。
- 定期或阈值再平衡。
- 最低现金比例。
- 单票和总仓位约束。
- 最大持仓数量。
- 多标的逐笔成交和订单。
- 组合级收益、风险、暴露和集中度分析。

单股 `BacktestEngine` 继续保留，用于快速研究和策略调试。

## 2. 显式目标权重

```python
from core import PortfolioBacktestEngine, PortfolioConstraints

engine = PortfolioBacktestEngine(
    data,                     # {股票代码: DataFrame}
    target_weights,           # DataFrame，索引为日期，列为股票代码
    rebalance_frequency="M",  # 每月再平衡
    constraints=PortfolioConstraints(
        max_weight=0.2,
        min_weight=0.02,
        max_positions=5,
        min_cash_weight=0.05,
        max_gross_exposure=0.95,
    ),
).run()
```

目标权重必须满足：

- 只允许 0 到 1 之间的非负权重。
- 不支持卖空和杠杆。
- 单票权重受 `max_weight` 限制。
- 总目标仓位受 `max_gross_exposure` 和 `min_cash_weight` 限制。
- 超过最大持仓数的标的会自动剔除。

第 t 日收盘后确定的目标权重，最早在第 t+1 日开盘执行，避免前视偏差。

## 3. 从策略信号生成组合

```python
engine = PortfolioBacktestEngine.from_signals(
    data,
    signals,                  # {股票代码: SignalOutput 或 (entries, exits)}
    allocation="equal_weight",
    rebalance_frequency="M",
    constraints=PortfolioConstraints(
        max_weight=0.2,
        min_cash_weight=0.05,
    ),
).run()
```

当前信号分配方式为 `equal_weight`：

- 买入信号使标的进入活跃集合。
- 卖出信号使标的退出活跃集合。
- 活跃标的平均分配可投资仓位。
- 单票超过最大权重时保留现金，不自动超配其他标的。

## 4. 再平衡

支持：

- `daily`：每个交易日检查。
- `weekly`：每周首个交易日。
- `monthly`：每月首个交易日。
- `quarterly`：每季度首个交易日。
- 任意正整数：每隔 N 根 K 线。

目标权重发生变化时，可以立即触发下一根 K 线再平衡。

`rebalance_threshold` 用于控制漂移再平衡。当实际权重与目标权重偏差低于阈值时，
定期再平衡会跳过交易，以减少成本。

目标权重是再平衡时约束，不是逐日强制上限。标的价格变化后，实际权重可能在
两次再平衡之间自然偏离目标。

## 5. 现金管理

- 所有标的共享一个现金账户。
- 卖出资金在同一交易周期优先用于买入。
- `min_cash_weight` 保留最低现金比例。
- 现金不足时买入订单只完成可负担部分，剩余订单继续等待。
- 不允许融资买入。

## 6. 成交和费用

组合引擎复用单股现实成交模型：

- 最低 5 元佣金。
- 基础滑点和成交量冲击成本。
- 成交量参与率。
- 部分成交和剩余挂单。
- T+1。
- 涨跌停排队。
- 停牌和零成交量。

组合成交明细包含股票代码、订单号、方向、数量、价格、费用和滑点。

## 7. 组合风险指标

`get_metrics()` 返回：

- 累计收益率和年化收益率。
- 年化波动率和夏普比率。
- 最大回撤和卡玛比率。
- 胜率和盈亏比。
- 总手续费。
- 成交笔数和部分成交订单数。
- 累计换手率。
- 平均现金比例。
- 最大总仓位和最大单票权重。
- 平均持仓集中度 HHI。
- 平均资产相关性。
- 日 VaR 95% 和日 CVaR 95%。

`get_pnl_contribution()` 返回每个标的的净现金流、期末市值、组合盈亏和贡献率。

`get_correlation_matrix()` 返回标的收益相关系数矩阵。

## 8. 查询接口

- `get_equity()`
- `get_cash()`
- `get_positions()`
- `get_weights()`
- `get_target_weights()`
- `get_fills()`
- `get_orders()`
- `get_trades()`
- `get_risk_metrics()`
- `get_pnl_contribution()`
- `get_correlation_matrix()`

## 9. 快捷入口

```python
from core import run_portfolio_backtest

engine = run_portfolio_backtest(
    data,
    target_weights,
    rebalance_frequency="M",
).run()
```

单股快速回测仍使用：

```python
from core import BacktestEngine
```

或：

```python
from core.engine import run_backtest
```

## 10. 当前边界

- 当前仅支持多头现金股票组合。
- 不支持行业分类约束、期货对冲和融资融券。
- 目标权重由调用方或策略生成，组合引擎不自动做资产配置。
- 未成交订单在新一轮再平衡时会被替换。
- 当前没有组合优化器和风险平价权重生成器。

