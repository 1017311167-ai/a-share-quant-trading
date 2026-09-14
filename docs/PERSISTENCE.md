# 统一交易持久化

## 1. 目标

统一 SQLite 数据库保存实盘运行所需的核心对象：

- 策略实例。
- 策略信号。
- 订单意图。
- 本地委托和券商委托映射。
- 成交。
- 当前持仓和持仓快照。
- 当前资金和资金快照。
- 配置版本。
- 风控事件和当前风险状态。
- 对账批次、差异、恢复记录和审计事件。

数据库默认位于项目可写的数据目录。首版仍以单机、单账户和模拟盘为主，
WAL 模式允许订单执行存储与统一仓储使用同一个 SQLite 文件。

## 2. 权威顺序

账户事实源的优先级固定为：

`券商订单/成交/资金/持仓 > 本地持久化状态 > 进程内存状态`

本地数据库用于审计、幂等和恢复，不能覆盖券商已经确认的成交。

## 3. 主要表

| 表 | 内容 | 幂等/主键 |
| --- | --- | --- |
| `strategy_instances` | 策略实例、版本、参数和状态 | `strategy_instance_id` |
| `signals` | 标准化策略信号 | `signal_id`，`idempotency_key` 唯一 |
| `order_intents` | 业务下单意图 | `order_intent_id`，`idempotency_key` 唯一 |
| `orders` | 本地委托与券商订单映射 | `local_order_id`，`client_order_id` 唯一 |
| `fills` | 不可变成交流水 | `broker_trade_id` 唯一 |
| `positions_current` | 当前持仓汇总 | `account_id + symbol` |
| `position_snapshots` | 持仓历史快照 | `snapshot_id` |
| `cash_current` | 当前资金汇总 | `account_id` |
| `cash_snapshots` | 资金历史快照 | `cash_snapshot_id` |
| `config_versions` | 配置版本和活动版本 | `config_name + version` |
| `risk_events` | 只追加风控事件 | `risk_event_id` |
| `risk_states` | 当前账户级风险状态 | `account_id` |
| `reconciliation_runs` | 对账批次 | `reconciliation_run_id` |
| `reconciliation_differences` | 对账差异及人工结论 | `difference_id` |
| `recovery_cases` | 崩溃恢复和确认记录 | `recovery_case_id` |
| `audit_events` | 人工操作和关键流程审计 | `audit_event_id` |

## 4. 事务规则

以下操作必须在一个事务内完成：

1. 保存信号及其下游意图。
2. 保存风控结论并更新意图状态。
3. 保存委托及其本地唯一键。
4. 保存成交并执行幂等检查。
5. 保存对账批次和差异。
6. 保存人工确认及其审计事件。

`SQLiteDatabase.transaction()` 使用 `BEGIN IMMEDIATE`，异常时整体回滚。
所有连接启用 WAL、`synchronous=FULL` 和外键。

## 5. 执行层桥接

现有 `OrderExecutionManager` 继续使用自己的执行表，保证状态机改动最小。
`ExecutionPersistenceBridge` 把执行订单和成交镜像到统一表：

```python
from execution import SQLiteExecutionStore
from persistence import (
    ExecutionPersistenceBridge,
    SQLiteDatabase,
    TradingRepository,
)

path = "data/trading.db"
execution_store = SQLiteExecutionStore(path)
repository = TradingRepository(SQLiteDatabase(path))
bridge = ExecutionPersistenceBridge(
    repository,
    account_id="模拟账号",
)

result = bridge.sync_manager(execution_manager)
```

重复同步是幂等的，不会重复插入成交。
首次应用成交到本地账本前必须先通过 `bootstrap_account()` 建立资金基线。
桥接会按买入手续费、卖出手续费和 T+1 可卖数量更新本地持仓与资金投影，
随后由账户对账继续核对券商事实。

## 6. 风控事件桥接

`RiskEngine` 支持可选 `event_sink`。`RiskEventPersistenceSink` 会在每次风控事件
产生后追加事件，并更新当前风险状态：

```python
from persistence import RiskEventPersistenceSink

sink = RiskEventPersistenceSink(
    repository,
    account_id="模拟账号",
    engine=lambda: risk_engine,
)
risk_engine.event_sink = sink
```

如果持久化失败，内存风控状态不会被撤销，失败信息记录在
`RiskEngine.last_persistence_error`，不会伪装成券商断线。

## 7. 当前边界

- SQLite 适合单机、单写者和模拟盘；正式实盘需评估服务型数据库。
- 金额当前仍以 `REAL` 存储，后续切换到固定精度字段前不能用于正式资金账本。
- 多账户隔离已经体现在账户主键和查询条件中，但现有 QMT 适配器仍是单账户。
- 当前不保存持仓批次和 T+1 可卖日期的完整 Lot，只保存汇总持仓与可用数量。
- 数据库迁移目前只做向后兼容的轻量列补齐，不是完整版本迁移框架。
