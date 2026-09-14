# 运行配置隔离

每个运行环境使用独立目录：

```text
configs/
  paper/
    common.env
    SIM-001.env
  research/
    common.env
```

- `common.env` 保存该环境共享配置。
- `<account>.env` 保存账户专属配置，并覆盖 common。
- 实际 `.env` 文件已被 `.gitignore` 忽略，只能提交 `.env.example`。
- `ops.service` 会校验 profile 和 account，禁止使用 `../` 跨目录读取。

启动示例：

```bash
python -m ops.service \
  --profile paper \
  --account SIM-001 \
  --metrics-port 9108 \
  -- --broker qmt --database data/paper_trading.db
```
