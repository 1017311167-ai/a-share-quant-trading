# 统一券商交易接口

## 1. 目标

交易执行层只依赖 `BaseBroker` 契约，不直接依赖 QMT SDK。当前提供：

- QMT 适配器 `QmtBroker`
- 内存模拟券商 `MockBroker`
- 统一订单和成交 DTO
- 统一回调事件
- 契约测试工具

统一接口首先服务模拟盘，不直接授权真实资金交易。

## 2. 统一数据模型

### OrderRequest

- symbol
- side
- order_type
- price
- quantity
- client_order_id
- strategy_name
- remark

首版只允许限价单。

### OrderSnapshot

- broker_order_id
- client_order_id
- account_id
- symbol
- side
- order_type
- price
- quantity
- filled_quantity
- remaining_quantity
- average_fill_price
- status
- order_sys_id
- submitted_at
- updated_at
- status_message
- strategy_name
- order_remark
- direction
- offset_flag
- raw_status
- raw

### TradeSnapshot

- trade_id
- broker_order_id
- client_order_id
- account_id
- symbol
- side
- quantity
- price
- amount
- commission
- stamp_tax
- transfer_fee
- total_fee
- traded_at
- order_sys_id
- direction
- offset_flag
- raw

### OrderDetail

- order
- trades

### BrokerEvent

- event_type
- account_id
- timestamp
- order
- trade
- error_code
- error_message
- raw

## 3. 基础接口

连接与生命周期：

- `connect`
- `disconnect`
- `reconnect`

下单与撤单：

- `submit_order`
- `order_buy`
- `order_sell`
- `cancel_order`
- `cancel_order_all`

查询：

- `get_order`
- `get_orders`
- `get_trades`
- `get_order_detail`
- `get_account_info`
- `get_positions`

行情：

- `subscribe_realtime`

`order_buy` 和 `order_sell` 保留用于兼容旧代码，内部应统一映射到 `OrderRequest`。

## 4. 订单状态

统一状态：

- `PENDING`
- `SUBMITTED`
- `PARTIALLY_FILLED`
- `FILLED`
- `CANCEL_PENDING`
- `CANCELLED`
- `REJECTED`
- `EXPIRED`
- `UNKNOWN`

QMT 数字状态通过 `normalize_order_status()` 转换。

## 5. 完整回调

订单回调必须转换为 `BrokerEvent(event_type="order", order=OrderSnapshot)`。

成交回调必须转换为 `BrokerEvent(event_type="trade", trade=TradeSnapshot)`。

其他事件：

- `connected`
- `disconnected`
- `order_error`
- `cancel_error`
- `account_status`
- `asset`
- `position`
- `order_async_response`

回调不能直接修改组合或持仓，必须交给应用层订单服务处理。

## 6. client_order_id

本地订单意图创建时生成稳定 `client_order_id`。

QMT 下单通过 `order_remark` 携带：

`client_order_id=<ID>|<业务备注>`

查询和回调通过备注恢复 client_order_id，用于程序重启后关联订单。

正式接入时还应校验券商的订单备注长度限制；如果备注被截断，需要使用本地订单号
与服务器回报的 order_sysid 建立映射。

## 7. 单笔撤单

`cancel_order(order_id)`：

- 调用券商单笔撤单。
- 成功返回 True。
- 失败抛 `BrokerOrderError`。
- 重复撤单或终态订单应按券商规则返回错误或 False。

批量撤单只对当前可撤订单操作，单个失败不能中断其他撤单。

## 8. 断线重连

`reconnect(max_attempts=3, delay=1.0)`：

1. 主动断开。
2. 按次数重试连接。
3. 成功后重新订阅账号。
4. 上层必须重新查询未终态订单、成交、持仓和资金。

重连成功不等于状态一致。恢复交易前必须完成对账。

## 9. MockBroker

`MockBroker` 使用内存资金、持仓、订单和成交，支持：

- `fill_mode="none"`：只接受订单。
- `fill_mode="full"`：自动全部成交。
- `fill_mode="partial"`：按比例部分成交。
- `fill_mode="reject"`：自动拒单。
- `fill_order()`：手工控制成交。
- `reject_order()`：手工废单。
- `simulate_disconnect()`：模拟断线。
- `push_quote()`：模拟实时行情。

典型测试：

```python
from broker_adapter import MockBroker, OrderRequest, OrderSide

broker = MockBroker(fill_mode="none")
broker.connect()
order_id = broker.submit_order(OrderRequest(
    symbol="600519",
    side=OrderSide.BUY,
    price=1500.0,
    quantity=100,
    client_order_id="intent-001",
))
broker.fill_order(order_id, quantity=50, price=1500.0)
partial = broker.get_order(order_id)
```

## 10. 契约测试

`broker_adapter/contract.py` 提供：

- `assert_broker_contract()`
- `assert_fill_contract()`

契约覆盖：

- 连接、断开和重连。
- 下单和订单查询。
- 部分成交和全部成交。
- 成交查询和订单详情。
- 单笔撤单。

运行：

```bash
python3 broker_adapter/test_contract.py
python3 broker_adapter/test_broker.py
```

## 11. 当前边界

- QMT 真实订单备注长度和查询接口仍需在 Windows 模拟账户验证。
- 部分成交的佣金字段取决于券商回报，缺失时保持 0，不在适配器中推算。
- MockBroker 只模拟简化账本，不能替代真实交易所撮合。
- 重连后必须由应用层完成订单、成交、持仓和资金对账。

