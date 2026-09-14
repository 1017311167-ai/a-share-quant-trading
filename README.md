# 📈 A股量化交易系统

面向中国 A 股现金股票的量化交易系统，目标是覆盖研究、回测、QMT 模拟盘、
小资金试运行和正式实盘。当前代码主要完成回测、参数寻优和 QMT 交易接口原型，
尚未达到可直接接入真实资金的完整交易闭环。

- 项目目标与边界：[docs/PROJECT_CHARTER.md](docs/PROJECT_CHARTER.md)
- 三阶段准入标准：[docs/TRADING_STAGES.md](docs/TRADING_STAGES.md)
- 目标系统架构：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 行情数据管线：[docs/DATA_PIPELINE.md](docs/DATA_PIPELINE.md)
- 策略元数据与实验复现：[docs/EXPERIMENTS.md](docs/EXPERIMENTS.md)
- 现实成交模型与成本敏感性：[docs/EXECUTION_MODEL.md](docs/EXECUTION_MODEL.md)
- 多标的组合回测：[docs/PORTFOLIO_BACKTEST.md](docs/PORTFOLIO_BACKTEST.md)
- 稳健性与过拟合评估：[docs/VALIDATION.md](docs/VALIDATION.md)
- 交易领域数据模型：[docs/DOMAIN_MODEL.md](docs/DOMAIN_MODEL.md)
- 订单状态机与恢复规则：[docs/ORDER_STATE_MACHINE.md](docs/ORDER_STATE_MACHINE.md)

> 首版仅支持沪深 A 股现金股票。明确暂不支持期货、期权、融资融券、卖空、
> 杠杆、北交所股票、ETF 和可转债。

## 技术栈

| 组件 | 用途 |
|------|------|
| [Streamlit](https://streamlit.io/) | 网页图形界面 |
| [PyQt6](https://www.riverbankcomputing.com/software/pyqt/) + [pyqtgraph](http://www.pyqtgraph.org/) | 桌面版界面（`python main.py --gui`） |
| [VectorBT](https://vectorbt.dev/) | 可选历史成交路径，用于兼容旧结果 |
| [AKShare](https://akshare.akfamily.xyz/) | A 股免费数据源（无需注册） |
| [Plotly](https://plotly.com/) | 网页版图表 |

## 目录结构

```
.
├── main.py                # 启动入口：python main.py（网页版）/ --gui（桌面版）
├── requirements.txt       # 依赖清单
├── gui/                   # 桌面界面层（PyQt6 + pyqtgraph）
│   ├── main_window.py     # 主窗口：5 个标签页 + 日志框 + 进度条（--test 冒烟测试）
│   ├── worker_thread.py   # 回测工作线程（QThread，计算不卡界面）
│   ├── common.py          # 共用组件：表格（排序/复制/导出）、图表、样式、日志桥
│   ├── launcher.py        # 桌面版启动脚本（也是 PyInstaller 打包入口）
│   ├── e2e_test.py        # 端到端联调测试：下载分钟数据→参数优化→一键回测→批量回测→推送
│   └── pages/             # 每个标签页一个文件：单股回测/参数寻优/批量回测/数据下载/设置
├── app/                   # 界面层（Streamlit 网页界面）
│   ├── research_console.py # 数据/回测/组合/稳健性/实验综合控制台
│   ├── streamlit_app.py   # 交互界面：回测参数设置 + 图表指标 + CSV 导出
│   └── app.py             # 旧入口（兼容，转发到 streamlit_app.py）
├── data/                  # 统一行情数据层
│   ├── service.py         # 日线/分钟线/实时行情统一服务
│   ├── models.py          # 请求、质量报告、快照和实时行情对象
│   ├── trading_calendar.py # A股交易日历与交易时段
│   ├── quality.py         # 清洗、异常值、缺失数据和停牌候选检查
│   ├── snapshots.py       # 不可变快照和版本清单
│   ├── providers.py       # AKShare 数据源适配
│   ├── loader.py          # 数据层公共入口
│   └── test_data_layer.py # 不联网的数据层回归测试
├── core/                  # 回测核心
│   ├── backtest_engine.py # 事件驱动回测引擎（兼容可选 VectorBT 路径）
│   ├── execution_model.py # 次日开盘、部分成交、排队、停牌和冲击成本
│   ├── cost_analysis.py   # 佣金/滑点/参与率成本敏感性
│   ├── portfolio_backtest.py # 多标的、目标权重、再平衡和组合风险
│   ├── optimizer.py       # 参数寻优（网格搜索 + 参数热力图）
│   ├── batch_backtest.py  # 批量回测（多股票×多策略并发，排序 + Excel 导出）
│   └── risk_analysis.py   # 风控与绩效分析（指标/回撤/月度年度/风控校验/报告）
├── optimization/          # 参数寻优（独立包，多进程加速）
│   ├── __init__.py        # 统一入口：optimize_parameters(method='grid'/'genetic')
│   ├── grid_search.py     # 网格搜索：穷举所有参数组合，按目标指标排序
│   └── genetic_algo.py    # 遗传算法：DEAP 进化寻优，每代输出最优解，种子可复现
├── notification/          # 消息推送
│   └── notifier.py        # 邮件（HTML/附件）+ 企业微信机器人（text/markdown），.env 配置
├── strategies/            # 策略库
│   ├── base.py            # 策略统一接口
│   ├── metadata.py        # 策略版本与参数 Schema
│   ├── signals.py         # 标准信号输出与信号哈希
│   ├── double_ma.py       # 双均线策略（金叉买入 / 死叉卖出）
│   ├── rsi_strategy.py    # RSI 超买超卖策略
│   ├── boll_strategy.py   # 布林带突破策略
│   ├── momentum.py        # 动量策略（N 日涨幅达标买入 / 跌破均线卖出）
│   ├── turtle_strategy.py # 海龟策略（唐奇安通道突破）
│   └── factory.py         # 策略工厂（按名称创建策略）
├── research/              # 策略研究与可复现实验记录
│   ├── experiments.py     # 实验指纹、结果、环境快照和重放
│   ├── validation.py      # 样本内外、Walk-forward、蒙特卡洛和过拟合风险
│   ├── test_research.py   # 离线实验复现测试
│   └── test_validation.py # 离线稳健性和过拟合测试
└── utils/                 # 工具
    ├── config.py          # A股交易规则常量（费用、T+1、涨跌停）
    ├── data_loader.py     # 旧行情入口兼容层
    └── versioning.py      # Git 代码版本和运行环境元数据
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 启动软件

研究控制台（推荐）：

```bash
python main.py
```

经典回测页面：

```bash
python main.py --legacy
```

桌面版（PyQt6 窗口界面，无需浏览器）：

```bash
python main.py --gui
```

启动后在界面里选择股票、日期区间和策略，点击「运行」即可。桌面版所有回测、寻优、下载任务都在后台线程执行，界面不会卡顿；结果表格支持点击表头排序、Ctrl+C 复制、右键导出 CSV/Excel。

**数据频率切换（日线 / 分钟线）**：单股回测、参数寻优、批量回测、数据下载四个页面都有「数据频率」下拉框，可无缝切换 日线 / 1 分钟 / 5 分钟 / 15 分钟 / 30 分钟 / 60 分钟。分钟线数据来自新浪行情，只提供最近约 5 个交易日（界面选分钟频率时会有橙色提示）。指标的年化（夏普、年化收益率等）会自动按数据频率折算。

**参数寻优一键应用**：寻优完成后点「▶ 用最优参数回测」按钮，最优参数连同股票代码、起止日期、数据频率、资金费用和 A 股规则开关会一键填到「单股回测」页并自动开始回测。

**回测完成自动推送**：回测页和批量回测页勾选「完成后自动推送」（或结果出来后点「📤 推送」按钮），结果摘要会自动发到邮件 / 企业微信机器人。渠道在 `.env` 中配置（复制 `.env.example` 填写），配置状态和测试按钮见「设置与关于」页。

> 手动启动网页版也可以：`streamlit run app/app.py`

## 打包成 Windows 单文件 exe

桌面版可以打包成一个双击即用的 exe 文件（在 Windows 上执行，macOS 同理）：

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name A股量化回测 gui/launcher.py
```

打包后 exe 生成在 `dist/` 目录。参数说明：

- `--onefile`：打包成单个 exe 文件
- `--windowed`：不弹出黑色命令行窗口
- `--name`：exe 文件名（中文名也支持）

**减小体积的技巧**：网页版用的 streamlit / plotly 等库桌面版用不到，打包时排除掉可以明显缩小体积：

```bash
pyinstaller --onefile --windowed --name A股量化回测 ^
  --exclude-module streamlit --exclude-module plotly ^
  --exclude-module matplotlib --exclude-module PIL ^
  --exclude-module scipy --exclude-module tkinter ^
  gui/launcher.py
```

（以上命令是 Windows CMD 写法，换行符是 `^`；PowerShell 用反引号 `` ` ``。）

注意：

- vectorbt 依赖 numba（编译器），打包后体积仍较大（约 100~200 MB），属正常现象
- 若杀毒软件误报或启动太慢，可改用 `--onedir` 文件夹模式（启动更快）
- **消息推送**：exe 同目录放一个 `.env`（内容同 `.env.example`）即可使用推送功能
- **行情缓存**：打包后默认缓存在临时解压目录（每次运行会丢），可设置环境变量
  `ABACKTEST_CACHE_DIR` 指向固定目录（如 `D:\abacktest_cache`）保存缓存
- **数据快照**：可通过 `ABACKTEST_SNAPSHOT_DIR` 指定不可变快照目录，通过
  `ABACKTEST_CALENDAR_DIR` 指定交易日历缓存目录
- **实验记录**：可通过 `ABACKTEST_EXPERIMENT_DIR` 指定实验清单和结果目录

## 如何运行测试

每个模块自带冒烟测试，直接运行对应文件即可；桌面版测试不需要联网，端到端测试需要联网下载行情（通知推送用 mock，不会真实发消息）：

```bash
python3 gui/main_window.py --test     # 桌面版冒烟测试（页面/图表/表格/线程/推送链路，不联网）
python3 data/test_data_layer.py       # 数据层测试（交易日历/质量/快照/实时行情，不联网）
python3 research/test_research.py     # 策略元数据/信号/实验复现测试（不联网）
python3 core/test_execution_model.py  # 现实成交与成本敏感性测试（不联网）
python3 core/test_portfolio_backtest.py # 组合回测目标权重/现金/风险测试（不联网）
python3 research/test_validation.py   # 样本内/外、Walk-forward、蒙特卡洛和过拟合测试
python3 gui/e2e_test.py               # 端到端联调：下载分钟数据→参数优化→一键回测→批量回测→推送
python3 utils/data_loader.py          # 数据层联网冒烟测试（旧兼容入口）
python3 core/backtest_engine.py       # 回测引擎测试
python3 strategies/factory.py         # 策略工厂测试（其余模块同理，都带 run_test）
```

## 常见问题

- **数据下载失败**：AKShare 的数据来自公开网页接口，偶尔网络波动会失败，稍等片刻重试即可。
- **分钟线只能下载最近几天**：新浪行情只提供最近约 5 个交易日的分钟线，把起止日期改到最近几天即可。
- **SSL 证书报错**：运行 Python 安装目录下的 `Install Certificates.command` 一次即可解决。
- **界面端口被占用**：`streamlit run app/app.py --server.port 8502` 换个端口。

## 开发状态

- [x] 项目骨架 + 环境搭建
- [x] 行情数据下载（akshare，含本地缓存）
- [x] 回测引擎（vectorbt，整手交易 + 佣金滑点）
- [x] 事件驱动成交模型：次日开盘、最低佣金、参与率、部分成交、停牌和涨跌停排队
- [x] 前视偏差修复与佣金/滑点/参与率成本敏感性分析
- [x] 多标的组合回测（目标权重、再平衡、现金管理和组合风险）
- [x] 样本内/样本外、滚动、Walk-forward、参数稳定性、蒙特卡洛和过拟合风险
- [x] 风控与绩效分析（完整指标 + 回撤/月度/年度 + 风控校验 + 报告）
- [x] A股交易规则（T+1 / 涨跌停 / 印花税过户费，界面可开关）
- [x] 策略库：双均线 / RSI / 布林带（含策略工厂）
- [x] Web 交互界面（净值/回撤图 + 指标 + 交易明细 + CSV 导出）
- [x] 参数寻优（网格搜索 + 热力图，一键用最优参数回测）
- [x] 批量回测（多股票 × 多策略并发，排序对比 + Excel 导出）
- [x] 参数寻优包（网格搜索 + 遗传算法，统一入口，多进程加速，种子可复现）
- [x] 消息推送（邮件 + 企业微信机器人，渠道可开关，失败不影响主程序）
- [x] 桌面版界面（PyQt6：5 个标签页，任务全部子线程执行，pyqtgraph 图表，
      表格排序/复制/导出，进度条 + 日志框，支持打包成 exe）
- [x] 更多策略：动量、海龟（策略库共 5 个，网页版/桌面版全部接入）
- [x] 统一策略元数据、参数 Schema、标准信号和策略版本机制
- [x] 可复现实验记录（代码版本、数据版本、参数、成本、结果和重放）
- [x] 回测完成自动推送（回测页/批量页：勾选自动推送或手动按钮，邮件 + 企业微信）
- [x] 分钟线行情（1/5/15/30/60 分钟，新浪数据源）+ 日线/分钟线无缝切换回测，
      年化指标按频率自动折算
- [x] 模块联调打通：界面调用批量回测/参数优化/高频数据、批量回测自动推送、
      寻优结果一键应用、异常友好提示不崩溃
- [x] 端到端自动化测试（gui/e2e_test.py）：下载分钟数据→参数优化→批量回测→推送通知全流程
