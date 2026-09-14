# 交易领域数据模型

## 1. 文档目的

本文档定义策略进入实盘后所需的领域对象、标识、字段、关系、事务边界和幂等规则。
它描述目标模型，不代表当前代码已经实现。

核心原则：

1. 信号、订单意图、委托、成交、持仓和资金变更必须可追踪。
2. 所有业务动作先持久化，再调用券商或产生外部副作用。
3. 券商成交与账户记录是最终事实来源。
4. 状态变化采用追加事件，不覆盖历史状态。
5. 相同业务输入重复到达时必须返回已有结果，不能重复下单或重复记账。

## 2. 值对象

| 对象 | 字段示例 | 规则 |
| --- | --- | --- |
| Money | amount, currency | 金额使用 Decimal，数据库使用 NUMERIC |
| Price | value, tick_size | 价格保留市场允许的精度 |
| Quantity | shares | 股票数量为整数股 |
| Instrument | symbol, exchange, board | 当前仅支持 A 股现金股票 |
| TimeRange | start, end, timezone | 业务时区 Asia/Shanghai，存储 UTC |
| AccountRef | account_id, account_type | 不同账户严格隔离 |
| DataVersion | snapshot_id, schema_version | 引用行情数据版本 |
| StrategyVersion | key, version, implementation_hash | 引用策略实现版本 |
| RiskPolicyVersion | policy_id, version | 引用冻结的风控规则版本 |

禁止在领域模型中直接使用浮点数表示正式资金金额。回测可以使用浮点加速，
实盘账本和订单金额必须使用 Decimal 或在数据库中固定精度。

## 3. 聚合关系

```text
Account
  |
  +-- StrategyInstance
        |
        +-- Signal
              |
              +-- OrderIntent
                    |
                    +-- RiskDecision
                    |
                    +-- Order -> OrderTransition
                          |
                          +-- Fill

Account
  +-- Position -> PositionLot
  +-- CashSnapshot
  +-- RiskEvent
  +-- ReconciliationRun
```

一个策略实例可以产生多个信号。

一个信号通常产生一个订单意图，但组合调仓信号可以产生多个订单意图。

一个订单意图在其有效期内只能有一个活动委托链。撤单重报产生新的 Order，
并通过 `replaces_order_id` 关联，不创建新的业务意图。

一个成交只能属于一个委托。

## 4. StrategyInstance

表示一个已冻结策略版本在特定账户上的运行实例。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| strategy_instance_id | UUID/ULID | 主键 |
| account_id | string | 所属账户 |
| strategy_key | string | 稳定策略标识 |
| strategy_version | string | 语义版本 |
| implementation_hash | string | 实现哈希 |
| parameter_json | JSON | 冻结参数 |
| data_version | string | 启动时约定的数据版本 |
| risk_policy_version | string | 风控策略版本 |
| status | enum | 生命周期状态 |
| state_version | integer | 每次状态变化递增 |
| started_at | UTC timestamp | 启动时间 |
| stopped_at | UTC timestamp | 停止时间 |
| last_heartbeat_at | UTC timestamp | 心跳 |
| stop_mode | enum | RUNNING / STOP_OPEN / REDUCE_ONLY / STOPPED |

生命周期：

`INITIALIZING -> READY -> RUNNING -> PAUSED -> STOPPING -> STOPPED`

异常状态：

`ERROR`

同一个账户、策略实例和标的在同一时刻只能有一个有效控制者。

## 5. Signal

表示策略在某个行情事件后生成的可审计信号。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| signal_id | UUID/ULID | 主键 |
| strategy_instance_id | UUID/ULID | 来源策略实例 |
| symbol | string | 标的 |
| action | enum | BUY / SELL / TARGET_WEIGHT / NOOP |
| signal_time | UTC timestamp | 生成时间 |
| bar_end_time | UTC timestamp | 对应 K 线结束时间 |
| data_version | string | 行情版本 |
| strategy_state_version | integer | 生成信号时的策略状态版本 |
| target_weight | decimal | 组合信号可选 |
| target_quantity | integer | 单标的信号可选 |
| idempotency_key | string | 唯一幂等键 |
| status | enum | 信号处理状态 |
| payload_hash | string | 信号内容哈希 |
| created_at | UTC timestamp | 入库时间 |

状态：

`GENERATED -> ACCEPTED -> CONVERTED`

异常分支：

`DEDUPLICATED / REJECTED / EXPIRED`

幂等键建议：

`strategy_instance_id + bar_end_time + symbol + action + strategy_state_version + data_version`

同一幂等键只允许存在一个信号。重复行情回放、主进程重启或事件总线重投时，
系统返回已有信号，不继续创建订单。

## 6. OrderIntent

表示组合和风控层希望执行、但尚未成为券商委托的业务意图。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| order_intent_id | UUID/ULID | 主键 |
| signal_id | UUID/ULID | 来源信号 |
| account_id | string | 账户 |
| symbol | string | 标的 |
| side | enum | BUY / SELL |
| order_type | enum | LIMIT |
| limit_price | decimal | 限价 |
| requested_quantity | integer | 请求数量 |
| target_position_after | integer | 预期目标持仓 |
| risk_policy_version | string | 风控版本 |
| idempotency_key | string | 意图唯一键 |
| status | enum | 意图状态 |
| expires_at | UTC timestamp | 失效时间 |
| created_at | UTC timestamp | 创建时间 |
| updated_at | UTC timestamp | 更新时间 |

状态：

`PENDING_RISK -> APPROVED -> CONSUMED`

异常分支：

`REJECTED / EXPIRED / CANCELLED`

一个意图只有在 `APPROVED` 后才能创建 Order。

## 7. RiskDecision

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| risk_decision_id | UUID/ULID | 主键 |
| order_intent_id | UUID/ULID | 被检查的意图 |
| decision | enum | APPROVE / REJECT / RESIZE |
| approved_quantity | integer | 批准数量 |
| policy_version | string | 风控版本 |
| checks_json | JSON | 各规则结果 |
| reasons_json | JSON | 拒绝或缩量原因 |
| decided_at | UTC timestamp | 决策时间 |

同一订单意图可以保留多次风控决策，但只有最新有效决策可以驱动订单。

## 8. Order

表示本地订单聚合，对应一个券商委托。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| order_id | UUID/ULID | 本地主键 |
| broker_order_id | string | 券商订单号，提交前为空 |
| client_order_id | string | 本地幂等 ID，写入券商备注 |
| order_intent_id | UUID/ULID | 来源意图 |
| account_id | string | 账户 |
| symbol | string | 标的 |
| side | enum | 买入/卖出 |
| order_type | enum | 首版仅限价 |
| limit_price | decimal | 委托价 |
| requested_quantity | integer | 原始数量 |
| cumulative_filled_quantity | integer | 累计成交 |
| remaining_quantity | integer | 剩余数量 |
| average_fill_price | decimal | 加权均价 |
| status | enum | 订单状态机状态 |
| broker_status | string | 券商原始状态 |
| replaces_order_id | UUID/ULID | 撤单重报来源 |
| rejection_code | string | 废单原因 |
| rejection_message | string | 废单说明 |
| created_at | UTC timestamp | 创建时间 |
| submitted_at | UTC timestamp | 提交时间 |
| last_event_at | UTC timestamp | 最后事件 |
| terminal_at | UTC timestamp | 终态时间 |
| version | integer | 乐观锁版本 |

业务不变量：

- `0 <= cumulative_filled_quantity <= requested_quantity`
- `remaining_quantity = requested_quantity - cumulative_filled_quantity`
- 终态订单不能直接修改成交数量。
- 成交必须通过新的 Fill 和状态迁移写入。

## 9. OrderTransition

不可变订单状态事件。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| transition_id | UUID/ULID | 主键 |
| order_id | UUID/ULID | 订单 |
| sequence_no | integer | 订单内严格递增 |
| from_status | enum/null | 原状态 |
| to_status | enum | 新状态 |
| event_type | enum | 本地命令/券商事件/恢复事件 |
| source | enum | LOCAL / BROKER / RECOVERY / MANUAL |
| broker_event_id | string/null | 券商唯一事件 ID |
| broker_timestamp | UTC timestamp | 券商时间 |
| recorded_at | UTC timestamp | 本地入库时间 |
| payload_hash | string | 原始事件哈希 |
| payload_json | JSON | 原始事件 |

唯一约束：

`order_id + sequence_no`

如券商提供唯一事件 ID，还需增加：

`broker_event_id`

## 10. Fill

成交是独立不可变对象，不能只存在订单汇总字段中。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| fill_id | UUID/ULID | 本地主键 |
| broker_trade_id | string | 券商成交编号 |
| order_id | UUID/ULID | 关联订单 |
| account_id | string | 账户 |
| symbol | string | 标的 |
| side | enum | 买入/卖出 |
| filled_quantity | integer | 本次成交数量 |
| fill_price | decimal | 成交价 |
| gross_amount | decimal | 成交金额 |
| commission | decimal | 佣金 |
| stamp_tax | decimal | 印花税 |
| transfer_fee | decimal | 过户费 |
| total_fee | decimal | 总费用 |
| filled_at | UTC timestamp | 成交时间 |
| received_at | UTC timestamp | 本地接收时间 |
| raw_payload_hash | string | 原始回调哈希 |

唯一约束：

`account_id + broker_trade_id`

如果没有券商成交编号，使用：

`broker_order_id + symbol + filled_at + quantity + price + raw_hash`

重复回调命中唯一约束时必须忽略副作用。

## 11. Position 与 PositionLot

Position 表示当前汇总持仓。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| position_id | UUID/ULID | 主键 |
| account_id | string | 账户 |
| symbol | string | 标的 |
| total_quantity | integer | 总持仓 |
| sellable_quantity | integer | 可卖数量 |
| frozen_quantity | integer | 冻结数量 |
| average_cost | decimal | 平均成本 |
| market_price | decimal | 最新价格 |
| market_value | decimal | 市值 |
| realized_pnl | decimal | 已实现盈亏 |
| unrealized_pnl | decimal | 浮动盈亏 |
| updated_at | UTC timestamp | 更新时间 |
| version | integer | 乐观锁版本 |

PositionLot 用于表达 A 股 T+1 可用性和批次成本：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| lot_id | UUID/ULID | 主键 |
| position_id | UUID/ULID | 关联持仓 |
| source_fill_id | UUID/ULID | 来源买入成交 |
| acquired_at | UTC timestamp | 买入日期 |
| available_at | UTC timestamp | 可卖时间 |
| original_quantity | integer | 原始数量 |
| remaining_quantity | integer | 剩余数量 |
| cost_price | decimal | 批次成本 |

买入成交产生 Lot；卖出成交按规则消耗 Lot。

## 12. CashSnapshot

CashSnapshot 是某一时点的不可变资金记录。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| cash_snapshot_id | UUID/ULID | 主键 |
| account_id | string | 账户 |
| snapshot_time | UTC timestamp | 快照时间 |
| total_asset | decimal | 总资产 |
| available_cash | decimal | 可用资金 |
| frozen_cash | decimal | 冻结资金 |
| market_value | decimal | 持仓市值 |
| receivable | decimal | 应收 |
| payable | decimal | 应付 |
| source | enum | BROKER / LOCAL / RECONCILED |
| reconciliation_run_id | UUID/ULID | 对账批次 |
| payload_hash | string | 原始响应哈希 |

本地现金不能覆盖券商资金快照。对账后以券商快照为准，本地差异写入审计事件。

## 13. RiskEvent

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| risk_event_id | UUID/ULID | 主键 |
| account_id | string | 账户 |
| strategy_instance_id | UUID/ULID | 策略实例，可为空 |
| order_intent_id | UUID/ULID | 订单意图，可为空 |
| symbol | string | 标的，可为空 |
| rule_code | string | 规则编码 |
| severity | enum | INFO / WARNING / CRITICAL |
| action | enum | ALLOW / REJECT / RESIZE / STOP_OPEN / REDUCE_ONLY / KILL |
| measured_value | decimal | 触发值 |
| threshold_value | decimal | 阈值 |
| status | enum | TRIGGERED / ACTIVE / RESOLVED |
| details_json | JSON | 详细证据 |
| triggered_at | UTC timestamp | 触发时间 |
| resolved_at | UTC timestamp | 恢复时间 |

风险事件只追加，不删除。恢复必须产生新的状态事件。

## 14. ReconciliationRun

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| reconciliation_run_id | UUID/ULID | 主键 |
| account_id | string | 账户 |
| started_at | UTC timestamp | 开始时间 |
| finished_at | UTC timestamp | 完成时间 |
| status | enum | RUNNING / MATCHED / MISMATCHED / FAILED |
| local_order_count | integer | 本地订单数 |
| broker_order_count | integer | 券商订单数 |
| local_position_count | integer | 本地持仓数 |
| broker_position_count | integer | 券商持仓数 |
| differences_json | JSON | 差异明细 |
| resolution_action | enum/null | 自动处理或人工等待 |

出现未解释差异时，进入 `RECONCILING` 并禁止开仓。

## 15. 幂等规则

| 对象 | 幂等键 |
| --- | --- |
| Signal | strategy_instance + bar_end_time + symbol + action + state_version + data_version |
| OrderIntent | signal_id + policy_version + approved_quantity + target_position_after |
| Order | order_intent_id + attempt_no |
| Fill | account_id + broker_trade_id |
| OrderTransition | order_id + sequence_no 或 broker_event_id |
| RiskEvent | rule_code + trigger_time + affected object + version |

幂等处理原则：

1. 先查询唯一键。
2. 已存在且内容哈希一致，直接返回已有对象。
3. 已存在但内容哈希不同，记录冲突并停止自动处理。
4. 任何幂等冲突都不能通过新订单绕过。

## 16. 事务边界

以下操作必须在一个本地事务中完成：

1. 保存 Signal 和对应 OrderIntent。
2. 保存 RiskDecision 并更新意图状态。
3. 创建 Order、首个 OrderTransition 和 client_order_id。
4. 保存 Fill、订单累计数量、PositionLot、Position 和 CashSnapshot。
5. 保存 ReconciliationRun 和差异解决结果。

本地事务提交成功后才能调用券商接口。

券商接口调用成功后，结果写入也必须幂等。

## 17. 查询模型

UI 和报表只读取查询模型，不直接读取交易聚合：

- 当前策略实例视图。
- 当前订单与活动挂单视图。
- 当日成交流水。
- 当前持仓与批次可用时间。
- 最新资金快照。
- 风控事件时间线。
- 对账结果和账户差异。
- 单订单完整事件链。

## 18. 数据保留

- Signal、OrderIntent、OrderTransition、Fill、RiskEvent 永久保留。
- Position 和 CashSnapshot 保留历史版本。
- 原始券商响应保留哈希和脱敏后的 JSON。
- 敏感账号、token 和完整个人信息不得写入事件 JSON。

## 19. 验收不变量

- 没有 RiskDecision 的意图不能创建 Order。
- 没有 Order 的 Fill 不能入账。
- 一个 broker_trade_id 只能产生一个 Fill。
- 一个 client_order_id 只能对应一个券商委托。
- 终态订单不能通过普通事件回退到非终态。
- 所有 Position 和 CashSnapshot 都能追溯到 Fill 或券商对账快照。
- 重启后可以从持久化事件恢复到与券商一致的状态。

