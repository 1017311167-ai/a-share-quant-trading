# 运行监控与运维

## 1. 结构化日志

默认日志文件为 `logs/runtime.jsonl`，每行一个 JSON：

```json
{"timestamp":"2026-09-14T10:00:00+00:00","level":"INFO","event":"signal_executed","context":{"account_id":"SI***01"}}
```

配置：

```dotenv
OPS_LOG_LEVEL=INFO
OPS_LOG_FILE=logs/runtime.jsonl
OPS_LOG_JSON=true
OPS_LOG_MAX_BYTES=10485760
OPS_LOG_BACKUP_COUNT=20
```

日志文件按大小轮转，默认保留 20 个备份。密码、Token、API Key、
Webhook 和账户号在输出前统一脱敏。

## 2. 监控指标

交易进程可以启动只读 HTTP 指标端点：

```bash
python -m trading.runner --metrics-host 127.0.0.1 --metrics-port 9108
```

端点：

- `GET /metrics`：Prometheus 文本格式。
- `GET /health`：进程健康检查。

主要指标：

- `trading_runtime_online`
- `trading_runtime_cycles_total`
- `trading_signals_total`
- `trading_orders_total`
- `trading_fills_total`
- `trading_risk_state`
- `trading_reconciliation_differences`
- `trading_command_total`

当前指标保存在交易进程内存中。进程重启后计数从零开始，风控和对账状态仍可从
SQLite 恢复。

## 3. 告警

`AlertManager` 统一处理：

- 券商断线。
- 恢复检查阻断。
- 对账差异。
- 全局急停。
- 进程异常重启。

相同规则默认 300 秒冷却，防止重复轰炸。告警内容发送前再次脱敏，并通过现有邮件或
企业微信 Notifier 推送。

```dotenv
OPS_ALERT_COOLDOWN_SECONDS=300
OPS_ALERT_STATE_FILE=logs/alert_state.json
```

## 4. 配置隔离

不同环境放在不同目录：

```text
configs/paper/common.env
configs/paper/SIM-001.env
```

账户配置覆盖 common。真实 `.env` 已被 Git 忽略，只提交 `.env.example`。
`ops.service` 会拒绝包含路径跳转的 profile 或 account。

```bash
python -m ops.service \
  --profile paper \
  --account SIM-001 \
  --metrics-port 9108 \
  -- --broker qmt --database data/paper_trading.db
```

## 5. 进程守护

```bash
python -m ops.supervisor \
  --stop-file logs/paper.stop \
  --state-file logs/supervisor.json \
  --max-restarts 50 \
  --restart-window 3600 \
  -- python -m ops.service --profile paper --account SIM-001 -- \
    --broker qmt --database data/paper_trading.db
```

子进程异常退出后使用指数退避自动重启，默认 1、2、4、8 秒，最高 60 秒。
一小时达到重启上限后守护进程停止，避免无限故障循环。

创建 `logs/paper.stop` 可以请求正常停止。

## 6. 备份与恢复

SQLite 使用在线备份 API，确保 WAL 模式下备份一致：

```bash
python -m ops.backup create \
  --database data/paper_trading.db \
  --backup-dir backups \
  --label daily \
  --retain 30
```

校验：

```bash
python -m ops.backup verify backups/paper_trading_daily_20260914T150000.db
```

恢复必须显式确认；恢复前会额外备份当前数据库：

```bash
python -m ops.backup restore \
  backups/paper_trading_daily_20260914T150000.db \
  --target data/paper_trading.db \
  --confirm
```

## 7. 监控建议

- 每 30 秒检查 `/health`。
- 每 1 分钟抓取 `/metrics`。
- `trading_runtime_online=0` 持续 60 秒时告警。
- 风险状态非 `active` 时告警。
- 对账差异大于零时告警。
- 每日收盘后自动备份，保留至少 30 天。
