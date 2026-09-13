# 行情数据管线

## 1. 目标

数据层负责把不同来源、不同频率的行情统一为可校验、可追溯、可复现的数据版本。
回测、策略研究、模拟盘和实盘必须通过同一组接口读取数据，不能自行解析数据源。

## 2. 数据流

```text
DataRequest
    |
    v
Snapshot lookup -> Legacy cache -> Provider download
    |                 |                  |
    +-----------------+------------------+
                      |
                      v
                Deterministic clean
                      |
                      v
                 Quality check
                      |
                      v
             Immutable snapshot + manifest
                      |
                      v
                   DataFrame
```

## 3. 标准数据格式

所有历史行情都使用固定六列：

| 列 | 类型 | 含义 |
| --- | --- | --- |
| date | datetime | 日线日期或分钟线时间 |
| open | float | 开盘价 |
| high | float | 最高价 |
| low | float | 最低价 |
| close | float | 收盘价 |
| volume | int | 成交量，统一为股 |

DataFrame 的 `attrs` 附加以下元数据：

- `data_version`
- `schema_version`
- `provider`
- `adjust`
- `snapshot_manifest`
- `data_quality`

列结构保持不变，因此现有回测和策略代码无需修改。

## 4. 统一接口

历史行情：

- `load_daily_data`
- `load_minute_data`
- `load_market_data`

版本和质量管理：

- `create_data_snapshot`
- `load_snapshot`
- `list_snapshots`
- `get_quality_report`
- `get_suspension_dates`

实时行情：

- `subscribe_realtime`
- `set_realtime_provider`

统一服务对象为 `MarketDataService`，默认服务由 `get_default_service()` 提供。
原 `utils.data_loader` 入口继续保留，内部转发到 `data` 包。

## 5. 复权

支持三种模式：

- `none`：不复权。
- `qfq`：前复权，默认模式。
- `hfq`：后复权。

复权模式属于快照身份的一部分。前复权缓存不会被错误复用于后复权或不复权请求。

## 6. 交易日历

交易日历来自 AKShare，并缓存到：

`data/cache/calendar/trade_dates.csv`

日历记录来源和版本哈希。无网络且无缓存时，非严格模式可以使用工作日兜底日历，
但质量报告会标记来源。严格模式 `strict=True` 不允许使用兜底日历。

交易日历用于：

- 判断有效交易日。
- 生成分钟交易时段。
- 检查缺失日线或分钟线。
- 识别疑似停牌日期。

实时调用 `refresh_trading_calendar()` 可以强制刷新日历。

## 7. 停牌和缺失数据

如果交易日历中存在交易日但行情数据缺失，系统将其记录为疑似停牌或数据缺失，
并在质量报告中分别保存数量和日期列表。

系统不会把缺失交易日自动填充为伪造 K 线。使用时必须明确选择：

- 在策略中跳过缺失日。
- 在组合和回测中按停牌处理。
- 重新下载或更换数据源。

## 8. 异常值检查

质量检查覆盖：

- 空数据。
- 缺少标准列。
- 无法解析的日期。
- 重复时间戳。
- 时间顺序错误。
- 非正价格。
- OHLC 关系不一致。
- 负成交量。
- 异常零成交量比例。
- 超过阈值的大幅价格跳变。
- 日线缺失交易日。
- 分钟线缺失交易时段。

价格异常只报告，不静默修改。清洗步骤只负责日期转换、排序、去重和成交量缺失填充。

## 9. 质量报告

质量报告包含：

- 数据量。
- 实际起止时间。
- 数据来源。
- 日历来源和版本。
- `PASS`、`WARN` 或 `FAIL` 状态。
- 0 至 100 的质量分数。
- 所有错误、警告和样例日期。

错误会阻止快照用于正式流程；警告允许研究使用，但必须保留报告。

## 10. 快照和版本

快照默认保存在：

`data/snapshots/<股票代码>/<频率>/<复权>/`

每个快照由 CSV 和 JSON manifest 组成。Manifest 记录：

- 快照 ID。
- 请求股票、频率、复权和起止日期。
- 实际数据范围。
- 行列数和内容 SHA-256。
- Schema 版本。
- 数据源。
- 交易日历来源和版本。
- 质量报告。
- 创建时间和代码版本。

相同请求和相同内容会得到稳定快照 ID。读取时优先使用权威 AKShare 日历生成的快照，
不会让工作日兜底快照覆盖真实日历版本。

## 11. 实时行情

实时行情统一返回 `RealtimeQuote`，包括代码、时间、最新价、开高低、昨收、成交量、
成交额、来源和原始数据。

实时提供器只需实现：

`subscribe_realtime(codes, callback)`

QMT broker 可以作为实时提供器注入。数据层负责把其原始字典转换为统一对象，
上层策略和监控模块不直接依赖 QMT 字段名。

## 12. 可复现性边界

快照可以证明某次研究使用了什么数据，但本地快照不会自动同步到其他机器。
需要长期复现时必须备份 `data/snapshots`，并在实验记录中保存 `data_version`。

如上游数据源修改历史数据、复权算法或交易日历，新下载内容会生成不同快照 ID，
旧快照不会被覆盖。

