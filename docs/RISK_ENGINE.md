# 实盘风控引擎

## 1. 目标

风控引擎位于订单意图和 Broker 下单之间。任何订单只有在风险检查通过后才能提交。

覆盖三层：

1. 交易前检查。
2. 交易中和账户级监控。
3. 全局状态控制。

## 2. 风险状态

| 状态 | 开仓 | 卖出 | 用途 |
| --- | --- | --- | --- |
| ACTIVE | 允许 | 允许 | 正常运行 |
| STOP_OPEN | 禁止 | 允许 | 单日亏损、行情或连接异常 |
| REDUCE_ONLY | 禁止 | 允许 | 最大回撤或人工降风险 |
| KILLED | 禁止 | 禁止 | 全局急停 |

状态只能由 `recover()` 人工恢复。连接恢复或行情恢复不会自动清除账户级限制。

## 3. 交易前检查

### 资金

- 可用资金必须大于 0。
- 买入金额不能超过可用资金。
- 扣除订单后必须保留最低现金比例。

### T+1

- 卖出数量不能超过 Position 的可用数量。
- `available_quantity=0` 时拒绝卖出。
- 请求超过可用数量时缩量到可卖数量。

### 整手

- 买入数量必须是 100 股整数倍。
- 卖出允许处理零股尾仓。

### 涨跌停

- 买入价不能高于涨停价。
- 卖出价不能低于跌停价。
- Quote 未直接提供涨跌停价时，使用前收盘价和板块规则计算。

### 单票仓位

- 买入后的单票市值不能超过 `max_single_position_pct`。
- 超过限额时计算允许的最大整手数量并缩量。

### 总仓位

- 所有持仓总市值不能超过 `max_gross_exposure_pct`。
- 买入后的总仓位超过限额时缩量。
- 账户不支持杠杆时总仓位不会超过 100%。

### 开仓时间

- 默认 14:50 后拒绝新的买入信号。
- 卖出和风险退出不受该时间限制。
- 模拟盘运行入口使用同一风控规则，不接受策略侧绕过。

账户级开仓截止时间不能替代交易日历。券商调用前还必须由
`TradingSessionGuard` 检查：

- 当天是否为交易日。
- 当前是否处于 `09:30-11:30` 或 `13:00-15:00`。
- 买卖委托、普通撤单和恢复补单是否允许执行。

全局急停的紧急撤单允许在非交易时段执行。

## 4. 交易中检查

### 行情中断

- Quote 超过 `max_quote_age_seconds` 时拒绝该标的订单。
- Quote 标记为不可交易或停牌时拒绝订单。

### 连接异常

- Broker 连接断开时立即进入 `STOP_OPEN`。
- 连接恢复后只产生恢复事件，不自动恢复交易。

### 单日亏损

- 每日开始时冻结起始权益。
- 当日收益低于 `-max_daily_loss_pct` 时进入 `STOP_OPEN`。
- 允许风险退出，不允许开仓。

### 最大回撤

- 峰值权益跨交易日保留。
- 当前权益相对峰值的回撤达到 `max_drawdown_pct` 时进入 `REDUCE_ONLY`。

## 5. 账户级控制

### 只减仓

`enable_reduce_only(reason)`：

- 拒绝买入。
- 允许卖出。
- 适用于回撤超限和人工降风险。

### 全局急停

`enable_kill_switch(reason)`：

- 拒绝买入和卖出。
- 上层订单服务必须同步撤销可撤订单。
- 必须人工确认恢复。

推荐的完整操作使用：

```python
cancelled = execute_kill_switch(broker, engine, "人工急停")
```

该函数会先进入 `KILLED`，再调用 Broker 的全部撤单，并记录撤单结果。

建议全局急停触发后：

1. 停止策略。
2. 调用 Broker `cancel_order_all()`。
3. 查询最终订单和成交。
4. 重新执行资金、持仓对账。
5. 人工确认后调用 `recover()`。

## 6. 默认阈值

`RiskLimits` 默认值：

- 单票最大仓位：20%
- 最大总仓位：80%
- 最低现金比例：5%
- 单日最大亏损：2%
- 最大账户回撤：8%
- 行情最大年龄：30 秒
- 每日最大订单数：50
- 买入交易单位：100 股

阈值属于风险策略版本，通过 `policy_hash` 记录。

## 7. 使用方式

```python
from risk import RiskEngine, RiskLimits, QuoteState
from broker_adapter import MockBroker, OrderRequest, OrderSide

broker = MockBroker(fill_mode="none")
broker.connect()

engine = RiskEngine(RiskLimits(
    max_single_position_pct=0.20,
    max_gross_exposure_pct=0.80,
    min_cash_pct=0.05,
))

decision, order_id = submit_with_risk(
    broker,
    engine,
    OrderRequest(
        symbol="600519",
        side=OrderSide.BUY,
        price=1500.0,
        quantity=100,
    ),
    {"600519": QuoteState(
        symbol="600519",
        last_price=1500.0,
        previous_close=1490.0,
    )},
)
```

`submit_with_risk()` 只适用于简单调用。正式订单服务仍负责持久化、幂等、订单状态机
和券商回调。

## 8. 风险事件

事件字段：

- rule_code
- level
- action
- status
- message
- symbol
- measured_value
- threshold_value
- occurred_at
- details

风险事件只追加，不删除。

## 9. 与 Broker 的适配

`risk_context_from_broker()` 自动读取：

- 账户资金
- 当前持仓
- 可卖数量
- 实时行情
- 连接状态

当前账户和持仓仍使用 Broker 的中文键 dict，后续可迁移为统一快照模型。

## 10. 当前边界

- 风控引擎不负责持久化订单和风险事件。
- 风控引擎不直接撤单，急停后需要订单服务执行全部撤单。
- 未实现行业集中度、风格暴露和组合相关性约束。
- 行情中断按标的拒绝订单，尚未自动区分全市场停市。
- 风险阈值修改必须通过发布流程，不应在交易时段动态调整。
