# 量化交易系统架构

## 1. 文档状态

本文档定义目标架构和依赖边界，不代表相关模块已经实现。当前代码仍以回测、
参数寻优和 QMT 接口原型为主。后续开发应按本文档逐步迁移，不要求一次性重构。

系统首版采用模块化单体架构。所有模块可以先运行在同一进程中，通过明确的接口、
事件和事务边界隔离，不预先拆分为微服务。

## 2. 架构目标

1. 研究与实盘复用同一套领域模型、策略接口、组合规则和风控规则。
2. 策略不能直接下单、访问数据库、读取券商账号或依赖图形界面。
3. 所有订单必须经过组合管理和风控，订单执行器不能绕过审批。
4. 券商是资金、持仓和成交的最终事实来源，本地记录用于恢复、审计和低延迟查询。
5. 回测、模拟盘和实盘只替换时钟、行情、券商、存储和监控适配器。
6. 模块依赖方向单向，业务核心不依赖 pandas、vectorbt、QMT、PyQt、Streamlit 等框架。
7. 异常时默认停止开仓，不能在状态未知时自动重试风险动作。

## 3. 分层架构

系统分为五层：

| 层 | 职责 | 可依赖 |
| --- | --- | --- |
| 领域层 Domain | 交易实体、值对象、状态机和业务不变量 | 仅标准库和领域内部模块 |
| 应用层 Application | 编排用例、事务边界、命令与查询 | Domain、Ports |
| 端口层 Ports | 定义行情、券商、存储、时钟、监控等接口 | Domain |
| 适配器层 Adapters | 实现 QMT、AKShare、数据库、通知等外部接口 | Ports、Domain、第三方 SDK |
| 表现层 UI | 展示状态、发送命令、查询视图 | Application 查询与命令接口 |

启动装配模块 Bootstrap 负责创建适配器并注入应用服务，是唯一允许同时依赖
Application、Ports 和 Adapters 的位置。

## 4. 模块划分

### 4.1 数据模块 Data

职责：

- 历史日线、分钟线和实时行情接入。
- 交易日历、交易时段、停牌和涨跌停状态。
- 数据标准化、复权、时区、去重和质量检查。
- 数据快照、版本和数据来源记录。
- 向应用层提供统一只读行情接口。

主要输出：

- `MarketSnapshot`
- `Bar`
- `Tick`
- `TradingSession`
- 数据质量报告

禁止依赖策略、订单执行、券商下单、UI 和回测引擎。

### 4.2 研究模块 Research

职责：

- 因子、指标和策略原型研究。
- 数据集切分、样本内外和滚动回测。
- 参数搜索、实验记录和结果对比。
- 策略准入申请和版本元数据。

主要输出：

- 实验配置与结果
- 策略候选版本
- 参数敏感性和样本外报告

研究模块可以依赖 Data 和 Backtest，但不能直接访问实盘券商或修改正式策略白名单。

### 4.3 回测模块 Backtest

职责：

- 向量化研究回测。
- 事件驱动行情回放。
- 模拟时钟、模拟成交和撮合模型。
- 组合净值、绩效和交易成本分析。
- 验证回测与实盘核心逻辑的一致性。

回测分为两个层次：

1. 向量化回测用于快速研究，不直接用于实盘。
2. 事件驱动回放使用与实盘相同的 Strategy Runtime、Portfolio、Risk 和 Execution。

最终实盘交易规则以事件驱动核心为准，不能直接调用向量化回测引擎下单。

### 4.4 策略运行模块 Strategy Runtime

职责：

- 加载并隔离策略实例。
- 向策略提供只读行情、组合视图和策略参数。
- 接收行情、成交、定时器和生命周期事件。
- 收集策略信号并生成策略建议。
- 保存和恢复策略内部状态。

策略接口必须区分：

- `initialize`：初始化只读上下文。
- `on_market`：处理行情事件。
- `on_fill`：处理成交回报。
- `on_timer`：处理时间事件。
- `snapshot` / `restore`：持久化和恢复状态。
- `stop`：安全退出。

策略只能输出信号或目标仓位建议，禁止直接创建券商订单、访问数据库或调用 UI。

### 4.5 组合管理模块 Portfolio

职责：

- 将多策略信号转换为组合目标仓位。
- 处理资金分配、持仓数量和最小交易单位。
- 计算目标仓位与当前仓位的差额。
- 处理现金、冻结资金和可卖数量。
- 在成交回报后更新组合视图。
- 提供组合级风险暴露和绩效数据。

主要输出：

- `TargetPortfolio`
- `PositionDelta`
- `OrderIntent`
- `PortfolioSnapshot`

组合模块负责决定“要持有什么”，但不决定如何向券商发送订单。

### 4.6 风控模块 Risk

职责：

- 交易前检查资金、持仓、价格、仓位、频率和账户状态。
- 交易中监控回撤、单日亏损、行情时效和连接状态。
- 计算允许的下单数量，或直接拒绝订单意图。
- 维护停止开仓、只减仓和全局急停状态。
- 记录风险决策、触发规则和恢复条件。

主要输出：

- `RiskDecision`
- `RiskEvent`
- `TradingState`

风控必须位于 Order Execution 之前。任何策略、UI 或适配器都不能绕过风控直接调用
券商下单接口。

### 4.7 订单执行模块 Execution

职责：

- 接收风控批准的订单意图。
- 生成、提交、跟踪和撤销券商订单。
- 管理订单状态机、幂等键和部分成交。
- 处理超时、撤单重报和未知状态。
- 将成交回报发送给组合、持久化和监控模块。

订单状态至少包括：

`CREATED -> APPROVED -> SUBMITTING -> SUBMITTED -> PARTIALLY_FILLED -> FILLED`

异常分支：

- `REJECTED`
- `CANCELLED`
- `EXPIRED`
- `UNKNOWN`
- `RECONCILING`

提交结果未知时禁止盲目重发，必须先查询券商订单和成交记录。

### 4.8 券商适配模块 Broker Adapter

职责：

- 实现券商连接、订单、撤单、资金、持仓和成交查询。
- 将券商 SDK 数据转换为领域对象。
- 将 SDK 异常转换为标准交易异常。
- 支持 QMT 模拟账户和实盘账户。

目标券商端口至少包括：

- 连接与断开。
- 限价买入和限价卖出。
- 单笔撤单和全部撤单。
- 订单查询和成交查询。
- 资金、持仓查询。
- 实时行情订阅。
- 连接和订单状态回调。

券商适配器不能生成交易信号、修改风控规则或直接更新组合状态。

### 4.9 持久化模块 Persistence

职责：

- 保存策略实例、配置版本和运行状态。
- 保存信号、订单意图、订单、成交和订单状态变化。
- 保存持仓快照、资金快照、风险事件和审计日志。
- 提供事务、幂等约束和崩溃恢复。
- 支持按策略、账户、标的和交易日查询。

主要概念：

- Repository：单个聚合的读写接口。
- Unit of Work：一次业务操作的事务边界。
- Event Store：订单、成交、风控和状态变化的不可变记录。

持久化不可用时，禁止提交新订单。建议首版使用 SQLite WAL 模式，
达到并发要求后再替换为 PostgreSQL 等独立数据库。

### 4.10 监控模块 Monitoring

职责：

- 记录结构化日志、指标和交易事件。
- 监控行情延迟、连接状态、订单积压和任务心跳。
- 监控资金、持仓、盈亏和风险状态。
- 发送风控告警、严重错误和运行状态通知。
- 提供只读审计和故障诊断数据。

监控模块只观察和报告，不得修改订单、持仓、风控状态或策略参数。

### 4.11 用户界面模块 UI

职责：

- 展示账户、持仓、订单、成交、策略和风险状态。
- 发送启动、停止、撤单、急停和只减仓命令。
- 展示研究、回测、模拟盘和实盘视图。
- 明确显示当前环境和账户，防止实盘误操作。

UI 只能依赖 Application 的命令和查询接口，禁止直接导入券商适配器、数据库会话、
策略实现或交易执行器。

## 5. 依赖方向

目标依赖图：

```text
UI
 |
 v
Application ---------> Ports <--------- Adapters
 |                       ^
 v                       |
Domain <-----------------+

Bootstrap -> Application + Ports + Adapters
```

允许的依赖：

- Domain 不依赖任何上层模块。
- Application 可以依赖 Domain 和 Ports。
- Ports 可以依赖 Domain 类型，但不能依赖具体适配器。
- Adapters 实现 Ports，可以依赖第三方 SDK。
- UI 只能依赖 Application。
- Bootstrap 负责所有对象装配。

禁止的依赖：

- Domain 不得导入 pandas、vectorbt、QMT、数据库驱动、GUI 或网络库。
- Strategy Runtime 不得导入 Broker Adapter、Persistence、UI。
- Portfolio 不得调用券商下单。
- Risk 不得调用券商下单或修改策略状态。
- Execution 不得生成策略信号。
- Broker Adapter 不得读取策略参数或自行决定是否交易。
- Monitoring 不得修改业务状态。
- UI 不得绕过 Application 直接访问底层模块。

## 6. 核心领域对象

详细字段、聚合关系和幂等约束见 `docs/DOMAIN_MODEL.md`；订单迁移和恢复流程见
`docs/ORDER_STATE_MACHINE.md`。

| 对象 | 含义 | 主要拥有者 |
| --- | --- | --- |
| Instrument | 股票标识、板块和交易规则 | Data / Domain |
| MarketEvent | 标准化行情事件 | Data |
| Signal | 策略产生的建议 | Strategy Runtime |
| TargetPortfolio | 组合目标仓位 | Portfolio |
| PositionDelta | 目标与当前仓位差异 | Portfolio |
| OrderIntent | 尚未提交的订单意图 | Portfolio / Execution |
| RiskDecision | 批准、拒绝或缩减结果 | Risk |
| Order | 券商委托状态 | Execution |
| Fill | 单笔成交记录 | Execution / Portfolio |
| Position | 当前持仓与可用数量 | Portfolio |
| AccountSnapshot | 资金和资产快照 | Broker / Portfolio |
| StrategyState | 可恢复的策略内部状态 | Strategy Runtime |
| AuditEvent | 不可变的审计事件 | Persistence / Monitoring |

## 7. 关键端口

| 端口 | 核心职责 | 典型实现 |
| --- | --- | --- |
| MarketDataPort | 历史行情、实时订阅、快照和健康状态 | AKShare、QMT、回放数据源 |
| BrokerPort | 下单、撤单、查询和回调 | QMT、模拟券商 |
| RepositoryPort | 聚合读写和事务 | SQLite、PostgreSQL |
| ClockPort | 当前时间和定时触发 | 系统时钟、模拟时钟 |
| TradingCalendarPort | 交易日和交易时段 | A 股交易日历 |
| MonitoringPort | 日志、指标、事件和告警 | 文件、监控平台、邮件 |
| ConfigPort | 版本化配置和阈值读取 | 环境变量、配置文件 |
| StrategyPort | 策略生命周期和信号输出 | 双均线、RSI 等 |

端口只定义能力，不包含券商 SDK、数据库或 UI 细节。

## 8. 交易事件流

正常交易流程：

1. 系统启动并加载已冻结的策略和风控配置。
2. 连接券商，执行资金、持仓和订单对账。
3. 对账通过后进入可交易状态，否则保持停止开仓。
4. MarketData 产生标准化行情事件。
5. Strategy Runtime 调用策略并产生 Signal。
6. Portfolio 将 Signal 转换为 TargetPortfolio 和 OrderIntent。
7. Risk 检查订单意图并产生 RiskDecision。
8. 只有批准的订单意图才能进入 Execution。
9. Execution 先持久化订单意图，再通过 BrokerPort 提交。
10. 券商回调产生 OrderUpdate 和 Fill。
11. Execution 去重并持久化成交，Portfolio 更新持仓和资金视图。
12. Monitoring 记录指标、告警和审计事件。
13. UI 通过查询接口展示最新状态，不直接订阅底层适配器。

## 9. 查询与命令分离

命令路径：

`UI / Scheduler -> Application Command -> Domain / Execution`

查询路径：

`UI <- Application Query <- Repository / Portfolio View`

命令必须校验权限、版本和环境。查询可以直接读取持久化视图，但不得触发交易动作。

## 10. 事务与一致性

- 订单提交前必须持久化订单意图和初始状态。
- 券商应答、成交和订单状态变化必须原子写入本地记录。
- 券商回调必须按订单号和成交编号去重。
- 本地状态与券商状态不一致时标记为 `RECONCILING` 并停止开仓。
- 策略状态快照和策略实例版本必须一起保存。
- 事务失败时不得继续执行外部风险动作。

一致性优先级：

`券商成交与账户记录 > 本地持久化状态 > 内存运行状态`

## 11. 失败处理

| 故障 | 默认动作 |
| --- | --- |
| 行情延迟或中断 | 停止开仓，保留风险退出 |
| 券商连接断开 | 停止开仓，恢复后先对账 |
| 下单返回未知 | 标记 UNKNOWN，查询券商后再处理 |
| 重复回调 | 按唯一编号幂等忽略 |
| 本地与券商不一致 | 以券商为准，进入只减仓或急停 |
| 数据库不可用 | 禁止提交新订单 |
| 策略异常 | 停止该策略实例，不自动重启 |
| 风控不可用 | 所有新订单默认拒绝 |

## 12. 运行时与并发

- 一个实盘账户只能有一个订单写入者。
- UI 线程不得直接执行交易或阻塞式查询。
- 行情、策略、风控、执行、持久化和监控之间通过队列或事件总线通信。
- 同一标的同一策略的执行必须保持事件顺序。
- 所有定时任务使用可注入时钟，便于回测和测试。
- 首版采用单进程模块化运行，出现明确性能瓶颈后再拆进程。

## 13. 回测与实盘复用

事件驱动回测和实盘使用相同组件：

| 组件 | 回测适配器 | 实盘适配器 |
| --- | --- | --- |
| 时钟 | SimulatedClock | SystemClock |
| 行情 | HistoricalReplayFeed | QmtMarketData |
| 券商 | SimulatedBroker | QmtBroker |
| 持久化 | 内存或测试数据库 | SQLite / PostgreSQL |
| 监控 | 测试事件收集器 | 日志、指标和告警 |

Strategy Runtime、Portfolio、Risk 和 Execution 不得包含回测或实盘分支。

## 14. 目标目录结构

```text
src/quant_trading/
├── domain/          # 领域对象和状态机
├── application/     # 用例编排、命令和查询
├── ports/           # 外部能力接口
├── adapters/        # 券商、行情、持久化、监控实现
├── data/            # 数据服务和数据质量
├── research/        # 研究、实验和策略准入
├── backtest/        # 向量化和事件驱动回放
├── trading/         # 策略运行和交易调度
├── portfolio/       # 组合和仓位管理
├── risk/            # 风控引擎
├── execution/       # 订单执行和状态机
├── monitoring/      # 日志、指标和告警
├── ui/              # Streamlit / PyQt 表现层
└── bootstrap/       # 配置与依赖装配
```

现有顶层目录将逐步迁移到上述结构。迁移过程中必须保持行为和测试结果不变。

## 15. 阶段实现范围

模拟盘前必须实现：

- Domain、Data、Strategy Runtime、Portfolio、Risk、Execution 和 BrokerPort。
- 订单幂等、持久化订单状态、收盘对账和全局急停。
- 最小交易台查询视图和结构化监控。

小资金试运行前必须实现：

- 完整订单与成交查询。
- 崩溃恢复、断线恢复和差异对账。
- 风控告警、只减仓模式和运维手册。

正式实盘前必须实现：

- 高可用监控、备份恢复、发布回滚和权限控制。
- 完整的审计追踪、事故演练和容量评估。

## 16. 架构验收标准

- Data、Research、Backtest、Strategy Runtime、Portfolio、Risk、Execution、
  Broker Adapter、Persistence、Monitoring 和 UI 均有唯一职责。
- 模块依赖无环，UI 和适配器不能进入领域核心。
- 策略无法绕过组合管理和风控直接下单。
- 回测和实盘可以替换适配器而复用同一套交易核心。
- 订单、成交、持仓和资金具备明确的一致性和恢复策略。
- 所有外部依赖都通过端口进入系统。
- 架构文档与项目章程、三阶段准入标准没有冲突。
