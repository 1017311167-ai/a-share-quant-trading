# 交易台与研究工作台

## 1. 工作区划分

网页入口默认进入交易台，左侧可切换到研究工作台。

交易台关注运行中的账户和风险：

- 模拟盘或实盘环境标识。
- 账户总资产、可用资金、持仓市值和今日盈亏。
- 当前持仓、订单、成交和策略实例。
- 当前风险状态、风险事件和对账差异。
- 停止开仓、只减仓、立即对账和全局急停。
- 运行实例心跳、命令状态和活动配置版本。

研究工作台关注策略证据和可复现性：

- 回测结果。
- 样本内外和 Walk-forward。
- 参数稳定性与过拟合风险。
- 运行配置版本。
- 行情数据质量和快照。
- 完整实验台账。

## 2. 环境标识

页面顶部始终显示当前环境：

- `TRADING_STAGE=paper` 和 `QMT_TRADING_MODE=SIMULATION` 显示为“模拟盘”。
- `TRADING_STAGE=real/live` 或 `QMT_TRADING_MODE=REAL` 显示为“实盘”。
- 未声明环境时显示为“研究环境”。

当前代码基线只允许模拟盘。交易台继续显示真实环境配置，便于发现误配置，但运行进程
会在连接和下单之前拒绝真实资金模式。

## 3. 运行心跳

交易进程在每个循环写入 `runtime_heartbeats`。页面在 30 秒内收到心跳时显示“在线”，
否则提示数据可能过期。

交易台不会直接修改内存中的 RiskEngine，也不会直接调用 QMT。控制按钮只写入
`runtime_commands`：

- `stop_open`
- `reduce_only`
- `kill_switch`
- `reconcile`

常驻交易进程认领命令后执行，并返回 `completed` 或 `failed`。全局急停仍经过统一
RiskEngine 和 `execute_kill_switch()`，不会绕过风控。

## 4. 启动

```bash
python main.py
```

等价入口：

```bash
streamlit run app/research_console.py
```

交易数据库可通过 `ABACKTEST_TRADING_DB` 指定。未指定时依次查找：

```text
data/paper_trading.db
data/paper_smoke.db
data/trading.db
```

## 5. 测试

```bash
python3 app/test_console.py
python3 trading/test_paper_runtime.py
```

测试覆盖双工作区渲染、模拟盘标识、急停二次确认、命令落库，以及运行进程对命令的
认领和执行。
