# CI 与发布流程

## 1. 持续集成

`.github/workflows/ci.yml` 包含两个阶段：

`unit`：

- Python 3.11 和 3.12 矩阵。
- 全仓编译。
- 运维、持久化、执行、风控和券商契约测试。

`integration`：

- 模拟盘完整信号、订单、成交、账户、对账和恢复测试。
- Web 交易台与研究台测试。
- MockBroker 模拟盘运行入口烟测。

CI 使用精简依赖 `requirements-ci.txt`，不会在 CI 中连接 QMT 或真实资金。

## 2. 本地执行等价流程

```bash
pip install -r requirements-ci.txt
python -m compileall -q .
python ops/test_ops.py
python persistence/test_persistence.py
python execution/test_execution.py
python risk/test_risk.py
python broker_adapter/test_contract.py
python broker_adapter/test_broker.py
python trading/test_session_guard.py
python trading/test_paper_runtime.py
python app/test_console.py
```

## 3. 发布包

```bash
python scripts/package_release.py --version v0.2.0 --output-dir dist
```

发布包包含运行代码、配置模板、文档和 Windows 脚本，不包含：

- `.env` 和账户密钥。
- SQLite 数据库。
- 日志和备份。
- 行情缓存、实验和运行产物。

包内包含：

- `release-manifest.json`
- `checksums.sha256`

## 4. Tag 发布

推送 `v*` Tag 后，`.github/workflows/release.yml` 会：

1. 安装 CI 依赖。
2. 运行运维、持久化、模拟盘和 UI 测试。
3. 创建带校验和的发布 ZIP。
4. 上传 CI Artifact。
5. 创建 GitHub Release 并生成发布说明。

```bash
git tag v0.2.0
git push origin v0.2.0
```

发布前必须确认：

- CI 全部通过。
- 模拟盘完整链路通过。
- Windows 部署包在模拟账户环境验证。
- 没有真实密钥或数据库进入发布包。
- 回滚版本和备份已经准备。
