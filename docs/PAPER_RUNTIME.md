# QMT 模拟盘持续运行

## 1. 当前边界

本阶段只允许 QMT 模拟账户或 MockBroker：

- `TRADING_STAGE=paper`。
- `QMT_TRADING_MODE=SIMULATION`。
- `QMT_ALLOW_REAL_TRADING=false`。
- Broker 必须声明 `is_simulation=True`。

任何真实阶段配置、非模拟 Broker 或真实交易开关都会被
`trading.safety.assert_paper_trading()` 拒绝。代码拒绝发生在连接和下单之前。

## 2. 完整链路

```text
JSONL 策略信号
  -> Signal 持久化与幂等
  -> 交易前风控
  -> OrderIntent
  -> OrderExecutionManager
  -> QMT 模拟账户
  -> 订单/成交查询
  -> 本地成交、持仓和资金账本
  -> 统一持久化
  -> 周期对账和差异告警
```

策略不能直接调用 QMT 下单接口。模拟运行宿主创建的执行管理器启用了
`require_risk=True`，没有 RiskEngine 的执行通道会直接报错。

## 3. QMT 配置

在 `.env` 中至少配置：

```dotenv
TRADING_STAGE=paper
QMT_TRADING_MODE=SIMULATION
QMT_ALLOW_REAL_TRADING=false
QMT_USERDATA_PATH=D:\迅投极速交易终端\userdata_mini
QMT_ACCOUNT=模拟资金账号
QMT_ACCOUNT_TYPE=STOCK
```

`QMT_ACCOUNT` 必须是操作人员已经确认过的模拟账户。QMT SDK 本身不一定能
从账号字符串判断实盘或模拟，因此环境配置和人工确认都是安全门禁的一部分。

## 4. 启动

首次运行需要操作人员核对券商资金和持仓，并建立本地账本基线：

```bash
python3 -m trading.runner \
  --broker qmt \
  --account 模拟资金账号 \
  --database data/paper_trading.db \
  --signal-file data/paper_signals.jsonl \
  --symbols 600519,000001 \
  --bootstrap \
  --operator operator@example.com \
  --notify
```

`--bootstrap` 只用于首次建立或人工确认重建账户基线。启动恢复、断线重连和
崩溃重启之后，仍需由 `--operator` 查看恢复检查并再次确认。

不连接 QMT 的本地烟测：

```bash
python3 -m trading.runner \
  --broker mock \
  --mock-fill-mode full \
  --database data/paper_smoke.db \
  --signal-file data/paper_signals.jsonl \
  --bootstrap \
  --operator local-test \
  --mock-clock 2026-09-14T10:00:00 \
  --mock-quote 600519=10.00 \
  --max-cycles 1
```

## 5. 信号文件

信号文件是 JSON Lines，每行一个信号。研究进程可以持续追加，交易进程按增量读取：

```json
{"symbol":"600519","action":"buy","quantity":100,"limit_price":10.0,"strategy_instance_id":"double-ma-1","idempotency_key":"double-ma-1:600519:20260914:buy","signal_time":"2026-09-14T10:00:00","data_version":"daily-20260914"}
```

相同 `idempotency_key` 重复出现时返回已有信号和订单，不会重复下单。

## 6. 持续检查

运行循环持续执行：

1. 读取新增信号。
2. 校验运行恢复状态和模拟环境。
3. 使用最新行情执行交易前风控。
4. 提交或复用已有订单。
5. 轮询订单和成交。
6. 同步统一订单、成交、持仓和资金记录。
7. 按配置周期执行账户对账。
8. 发现差异时进入 `STOP_OPEN` 并发送告警。

断线后交易状态进入 `BLOCKED`。重连不会自动恢复交易，必须重新创建恢复记录并
由操作人员再次确认。

## 7. 回放与故障注入

`trading.replay.ReplayRunner` 支持：

- `quote`：注入行情。
- `signal`：注入策略信号。
- `cancel`：撤单。
- `poll`：推进订单状态机。
- `reconcile`：执行账户对账。
- `disconnect`：模拟行情或券商断线。
- `reconnect`：重连并进入人工恢复检查。
- `settle`：MockBroker 执行 T+1 日终结算。

自动化测试还覆盖：

- 完整信号、下单、成交、持仓、资金和对账链路。
- 限价单撤单和部分成交。
- 重复信号不会重复下单。
- 重复成交同步不会重复增加持仓和扣减资金。
- 断线重连后保持停止开仓，必须再次人工确认。
- 无行情、超时开仓和未配置 RiskEngine 的绕过尝试都会被拒绝。
- 真实资金模式和非模拟 Broker 会被硬拒绝。

## 8. 验收命令

```bash
python3 trading/test_paper_runtime.py
python3 persistence/test_persistence.py
python3 execution/test_execution.py
python3 risk/test_risk.py
python3 broker_adapter/test_broker.py
```

当前机器若不是 Windows，QMT 真实模拟账户连接测试会自动跳过；必须在一台已登录
QMT 模拟账号的 Windows 主机上执行该联调，并保存运行日志、数据库和验收报告。
