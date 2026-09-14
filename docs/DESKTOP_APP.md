# macOS 桌面应用

## 1. 生成

```bash
python3 scripts/macos/build_app.py --output-dir dist
```

生成：

```text
dist/量化交易台.app
dist/启动量化交易台.command
```

## 2. 打开

在 Finder 中双击 `量化交易台.app`。应用会：

1. 使用当前机器架构启动 Python。
2. 建立独立的模拟盘演示数据库 `runtime/demo_paper.db`。
3. 自动选择空闲本地端口。
4. 启动交易台和研究工作台。
5. 自动打开默认浏览器。

演示账户为 `DEMO-PAPER-001`，所有数据只用于界面试运行。

## 3. 停止

关闭应用进程即可停止内部 Web 服务。也可以运行：

```bash
cat logs/launcher.json
```

查看当前 URL 和进程，然后使用系统“活动监视器”结束对应 Python 进程。

## 4. 接入真实模拟盘

.app 默认演示模式。连接 QMT 模拟账户时仍然使用：

```bash
python -m ops.service \
  --profile paper \
  --account 模拟账号 \
  --metrics-port 9108 \
  -- --broker qmt --database data/paper_trading.db
```

桌面演示包不会读取或修改真实 QMT 配置。
