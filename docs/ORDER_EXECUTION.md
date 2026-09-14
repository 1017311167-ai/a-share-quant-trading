# 订单执行管理器

## 1. 目标

`OrderExecutionManager` 位于风险决策和券商适配器之间，负责：

- 限价委托。
- 超时撤单。
- 有限追价。
- 部分成交。
- 失败重试。
- 幂等控制。
- 网络结果未知处理。
- 程序重启恢复。

执行管理器不生成策略信号，也不负责风控阈值判断。

## 2. 处理顺序

```text
OrderIntent
  -> 持久化业务意图
  -> 风险检查（可选）
  -> 创建本地 ManagedOrder
  -> 持久化本地订单
  -> 调用 Broker submit_order
  -> 查询订单和成交
  -> 更新状态、成交和终态
```

任何网络请求之前，本地意图和订单都必须已经提交到 SQLite。

## 3. 幂等键

业务意图幂等键为 `OrderIntent.idempotency_key`。

每次委托尝试生成：

`client_order_id = idempotency_key + ":" + attempt_no`

数据库约束：

- `execution_intents.idempotency_key` 唯一。
- `managed_orders.client_order_id` 唯一。
- `execution_fills.trade_id` 唯一。

相同的 `OrderIntent` 重复提交时，返回已有活动订单，不再次调用券商。

## 4. 执行状态

- `CREATED`
- `PENDING_SUBMIT`
- `SUBMITTING`
- `SUBMITTED`
- `PARTIALLY_FILLED`
- `CANCEL_PENDING`
- `UNKNOWN`
- `RECONCILING`
- `FILLED`
- `CANCELLED`
- `REJECTED`
- `EXPIRED`
- `FAILED`

终态订单不会重新提交。存在迟到成交时通过成交查询恢复。

## 5. 网络超时

提交过程中出现连接超时：

1. 标记订单为 `UNKNOWN`。
2. 不立即重发。
3. 等待 `unknown_grace_seconds`。
4. 查询券商订单和成交。
5. 如果券商确认不存在，才允许创建下一个 attempt。
6. 如果券商存在订单，恢复原订单状态。

同一个 `client_order_id` 永远不会用于两笔券商委托。

## 6. 程序重启

`recover()` 执行：

1. 加载所有非终态订单。
2. 按 `broker_order_id` 或 `client_order_id` 查询券商。
3. 恢复订单状态。
4. 补写缺失成交。
5. 对 `UNKNOWN` 订单执行安全对账。
6. 只对确认不存在于券商的订单创建新 attempt。

数据库和券商成交编号共同保证重启不会重复下单或重复记账。

## 7. 部分成交

- 券商累计成交优先于本地估算。
- 每笔成交按 `trade_id` 幂等保存。
- 剩余数量为请求数量减累计成交数量。
- 全部成交进入 `FILLED`。
- 部分成交且券商仍活动时进入 `PARTIALLY_FILLED`。

## 8. 超时撤单

订单超过 `cancel_after_seconds` 仍未成交或未完全成交时：

1. 持久化 `CANCEL_PENDING`。
2. 调用 Broker `cancel_order`。
3. 查询最终状态。
4. 成交先于撤单到达时先记账。
5. 撤单确认后进入 `CANCELLED`。

## 9. 有限追价

订单超过 `chase_after_seconds` 后允许有限追价：

- 买入向上追价。
- 卖出向下追价。
- 每次最多移动 `chase_step_ticks × price_tick`。
- 总追价次数不超过 `max_chase_steps`。
- 价格不能超过 `hard_limit_price` 或最大偏离比例。

执行流程：

1. 先撤销当前剩余订单。
2. 等待券商确认撤单。
3. 创建新 attempt。
4. 使用新的 client_order_id。
5. 继续执行剩余数量。

旧 attempt 不会与新 attempt 同时成为活动订单。

## 10. 失败重试

券商明确拒单时默认不自动重试。

设置 `retry_on_rejected=True` 后可以创建新 attempt，但必须满足：

- 仍有剩余数量。
- attempt 次数小于 `max_attempts`。
- 新 attempt 使用新的 client_order_id。

网络结果未知时，只有在券商确认原订单不存在后才允许重试。

## 11. SQLite 表

### execution_intents

- idempotency_key
- intent_id
- payload_json
- status
- created_at
- updated_at

### managed_orders

- local_order_id
- intent_id
- idempotency_key
- attempt_no
- client_order_id
- broker_order_id
- status
- payload_json
- created_at
- updated_at

### execution_events

- event_id
- local_order_id
- event_type
- created_at
- payload_json

### execution_fills

- trade_id
- local_order_id
- payload_json
- created_at

数据库启用 WAL 和 FULL 同步。

## 12. 使用方式

```python
from broker_adapter import MockBroker
from execution import (
    ExecutionPolicy,
    OrderExecutionManager,
    OrderIntent,
    SQLiteExecutionStore,
)

broker = MockBroker(fill_mode="none")
broker.connect()
store = SQLiteExecutionStore("data/execution.db")
manager = OrderExecutionManager(
    broker,
    store,
    policy=ExecutionPolicy(
        max_attempts=3,
        cancel_after_seconds=30,
        chase_after_seconds=5,
        max_chase_steps=2,
    ),
)
result = manager.submit_intent(OrderIntent(
    idempotency_key="strategy-001:600519:20260914",
    symbol="600519",
    side="buy",
    quantity=100,
    limit_price=1500.0,
))
```

## 13. 当前边界

- 当前只支持限价单。
- 追价策略是固定 tick 的有限追价，不包含自适应算法。
- 风险引擎为可选参数，尚未强制所有入口必须提供 RiskContext。
- 没有多进程共享时的账户级全局锁。
- SQLite 适合单机模拟盘；正式实盘建议迁移到服务型数据库。

