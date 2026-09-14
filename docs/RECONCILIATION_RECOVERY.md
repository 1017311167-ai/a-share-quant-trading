# 每日对账与崩溃恢复

## 1. 目标

每日收盘后以及程序重启后，系统必须先从券商查询订单、成交、持仓和资金，
再决定是否允许继续交易。发现无法解释的差异时：

- 禁止新开仓。
- 保留差异证据。
- 发送告警。
- 等待人工核查和确认。

## 2. 每日对账

```python
from persistence import ReconciliationService

service = ReconciliationService(
    repository,
    broker,
    risk_engine=risk_engine,
    notifier=notifier,
)
result = service.run_daily(account_id)
```

对账内容：

| 对象 | 检查 |
| --- | --- |
| 持仓 | 本地不存在、券商不存在、总数量/可卖数量不一致 |
| 资金 | 本地不存在、总资产/可用/冻结/市值超容差 |
| 委托 | 券商有本地无、本地有券商无、成交数量或状态不一致 |
| 成交 | 券商成交编号在本地缺失 |

差异类型以独立记录保存。即使下一轮查询发现当前账户已经一致，
未人工处理的旧差异仍会保持 `MISMATCHED`，不会被后续对账掩盖。

发现差异后：

1. 对账批次标记为 `MISMATCHED`。
2. 差异写入 `reconciliation_differences`。
3. 风险引擎进入 `STOP_OPEN`。
4. 通过邮件/企业微信发送差异摘要。

## 3. 差异处理

持仓和资金差异可以在人工核对后接受券商为权威状态：

```python
service.resolve_account_with_broker(
    account_id,
    resolved_by="operator@example.com",
    note="已核对券商资金和持仓，接受券商快照",
)
```

订单和成交差异不能通过上述方法静默清除。必须先执行订单恢复查询，
补写本地成交或状态，再重新对账。

单条差异也可以显式处理：

```python
service.resolve_difference(
    difference_id,
    resolved_by="operator@example.com",
    note="已核对券商成交回单",
    resolution="broker_authoritative",
)
```

## 4. 崩溃恢复

```python
from persistence import RecoveryService

recovery = RecoveryService(
    repository,
    broker,
    execution_manager=execution_manager,
    execution_bridge=bridge,
    reconciliation_service=service,
    risk_engine=risk_engine,
    required_configs=("execution", "risk"),
)
case = recovery.start(account_id, reason="process restart")
```

启动检查阶段只读：

- 不自动恢复执行管理器。
- 不自动下单、撤单或追价。
- 立即进入 `STOP_OPEN`。
- 检查券商连接、活动委托、账户差异和必需配置。

结果状态：

| 状态 | 含义 |
| --- | --- |
| `BLOCKED` | 存在差异、连接问题或不安全订单 |
| `READY_FOR_CONFIRMATION` | 账户一致，等待人工确认 |
| `CONFIRMED` | 已完成人工确认，允许恢复交易 |

## 5. 人工确认

```python
case = recovery.confirm(
    case["recovery_case_id"],
    confirmed_by="operator@example.com",
    note="账户和挂单已核对，同意恢复",
)
```

确认必须提供操作人员和说明。确认时会再次：

1. 恢复执行管理器订单状态。
2. 重新同步统一订单视图。
3. 重新执行账户对账。
4. 检查差异和不安全订单。
5. 全部通过后才调用 `RiskEngine.recover()`。

任何检查失败都会保持 `BLOCKED`，不会打开交易。

## 6. 验收标准

- 数据库重启后，策略、信号、订单、成交、持仓、资金、配置和风控事件仍可查询。
- 重复保存相同信号、意图和成交不会产生重复记录。
- 券商与本地一致时对账标记为 `MATCHED`。
- 任一账户差异都会产生差异记录、告警和 `STOP_OPEN`。
- 未处理的旧差异不会因后续对账一致而消失。
- 启动检查阶段不会调用下单或撤单接口。
- 有未处理差异时恢复记录保持 `BLOCKED`。
- 人工确认必须包含操作人和说明。
- 确认成功后风险状态才恢复为 `ACTIVE`。
