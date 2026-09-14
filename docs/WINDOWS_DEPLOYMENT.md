# Windows 部署

## 1. 环境准备

1. 安装 Python 64 位版本。
2. 安装并登录 QMT 模拟账户。
3. 确认 `xtquant` 可以正常导入。
4. 将项目放在固定目录，例如 `D:\quant`。

```powershell
cd D:\quant
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 2. 隔离配置

```powershell
Copy-Item configs\paper\common.env.example configs\paper\common.env
Copy-Item configs\paper\SIM-001.env.example configs\paper\SIM-001.env
```

编辑 `SIM-001.env`，填写已人工确认的模拟账号和 QMT 路径。

当前阶段必须保持：

```dotenv
TRADING_STAGE=paper
QMT_TRADING_MODE=SIMULATION
QMT_ALLOW_REAL_TRADING=false
```

## 3. 手工启动

```powershell
.\scripts\windows\run_paper.ps1 `
  -Profile paper `
  -Account SIM-001 `
  -Database D:\quant\data\paper_trading.db
```

该脚本通过 `ops.supervisor` 启动 `ops.service`，数据库异常、进程崩溃或 QMT 连接
进程退出后会自动重启。

## 4. 安装开机计划任务

以管理员身份打开 PowerShell：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\windows\install_paper_task.ps1 `
  -Profile paper `
  -Account SIM-001 `
  -Database D:\quant\data\paper_trading.db
```

默认任务名为 `AStockQuantPaperTrading`，在 Windows 启动时运行，并要求最高权限。

停止：

```powershell
.\scripts\windows\stop_paper.ps1
```

删除：

```powershell
.\scripts\windows\uninstall_paper_task.ps1
```

## 5. 指标与日志

- 结构化日志：`D:\quant\logs\runtime.jsonl`
- 守护状态：`D:\quant\logs\supervisor.json`
- Prometheus 指标：`http://127.0.0.1:9108/metrics`
- 健康检查：`http://127.0.0.1:9108/health`

生产环境建议限制 `9108` 端口仅本机或监控网段访问。

## 6. 每日备份

计划任务中可以增加：

```powershell
.\scripts\windows\backup_paper.ps1 `
  -Database D:\quant\data\paper_trading.db `
  -BackupDir D:\quant-backups `
  -Retain 30
```

备份完成后应校验 `checksums` 和服务器的磁盘告警。

## 7. 上线检查

- QMT 模拟账号已人工确认。
- `TRADING_STAGE=paper`、`QMT_TRADING_MODE=SIMULATION`。
- `QMT_ALLOW_REAL_TRADING=false`。
- 计划任务能够随 Windows 启动。
- 日志轮转和磁盘空间正常。
- `/health` 和 `/metrics` 可以访问。
- 断网、QMT 退出、进程崩溃和自动重启演练通过。
- 数据库备份可以校验并恢复到测试目录。
