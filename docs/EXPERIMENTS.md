# 策略元数据与可复现实验

## 1. 目标

每个策略、参数组合和回测结果都必须有稳定身份，能够回答：

- 使用了哪个版本的策略代码。
- 使用了哪些参数及其定义。
- 使用了哪一个行情数据版本。
- 使用了什么交易成本和 A 股规则。
- 产生了哪些信号和结果。
- 重跑时能否得到相同结果。

## 2. 策略元数据

每个策略包含：

- `strategy_key`：稳定英文标识。
- `version`：策略语义版本。
- `description`：策略说明。
- `parameter_schema`：参数类型、默认值、范围、步长和是否参与寻优。
- `required_columns`：所需行情列。
- `signal_schema_version`：信号格式版本。
- `tags`：策略分类。
- `constraints`：参数之间的约束说明。
- `implementation_hash`：策略源文件实现哈希。
- `metadata_hash`：元数据稳定哈希。

参数定义统一由 `ParameterSpec` 描述。界面和优化器不再需要自行猜测参数类型。
实现哈希可以在忘记升级语义版本时识别策略代码已经发生变化。

## 3. 策略参数

参数 Schema 支持 `int`、`float`、`bool` 和 `str`，可定义默认值、最小值、
最大值、开区间边界、枚举值和优化步长。

`Strategy.validate_params()` 会在创建策略前检查未知参数、类型、范围和枚举值。
跨参数约束仍由策略构造函数负责，例如双均线的 `fast < slow`。

## 4. 标准信号输出

旧接口继续有效：

```python
entries, exits = strategy.generate_signals(df)
```

新接口返回 `SignalOutput`：

```python
signal = strategy.generate_signal_output(df)
```

标准信号表包含：

| 列 | 含义 |
| --- | --- |
| date | 信号时间 |
| signal | 1 买入、-1 卖出、0 无动作 |
| entry | 买入布尔值 |
| exit | 卖出布尔值 |

`SignalOutput` 同时记录策略 key、版本、元数据哈希、参数哈希、数据版本、
信号内容哈希和买卖信号数量。信号哈希不包含生成时间。

## 5. 成本假设

`CostAssumptions` 统一记录初始资金、佣金、滑点、印花税、过户费、T+1、
涨跌停限制和无风险利率。成本假设属于实验输入指纹，任一变化都会产生新配置。

## 6. 实验记录

每个实验记录包含：

- 唯一实验 ID、实验类型和名称。
- 创建时间、代码版本、Python 和关键依赖版本。
- 行情数据版本、快照 ID、质量状态和日历版本。
- 策略元数据、参数和参数搜索范围。
- 成本假设和随机种子。
- 标准信号摘要、结果和结果哈希。
- 可复现输入指纹 `reproducibility_key`。

实验默认保存在 `experiments/<日期>/<实验 ID>/`，包含：

- `manifest.json`
- `results.csv`，参数搜索实验使用
- `signals.csv`，记录最佳或单次策略信号

## 7. 两个哈希

`reproducibility_key` 由代码版本、数据版本、策略元数据、参数、成本和随机种子
计算，用于判断两个实验是否使用完全相同的输入，不包含结果和运行时间。

`result_hash` 由结果表或绩效结果计算，用于判断重跑是否得到相同输出。

输入指纹相同但结果哈希不同，表示存在未记录的随机性、环境差异或计算错误。

## 8. 单次回测实验

```python
from research import run_backtest_experiment

result = run_backtest_experiment(
    df,
    "双均线",
    {"fast": 5, "slow": 20},
    code="600519",
    symbol="600519",
)
```

返回结果包含实验 ID、标准信号对象、回测引擎和结果哈希。
网页版和桌面版单股回测已经通过该入口自动记录实验。

## 9. 参数寻优实验

`optimization.optimize_parameters()` 默认自动记录实验，保存搜索方法、参数范围、
全部结果、最优参数、绩效指标和随机种子。无需记录时可传入
`record_experiment=False`。

旧入口 `core.optimizer.optimize()` 和批量回测 `core.batch_backtest.run_batch()`
也会记录，确保网页版和桌面版研究流程不会漏记。

## 10. 重放实验

```python
from research import reproduce_experiment

result = reproduce_experiment("实验ID")
print(result["matches"])
```

重放会读取实验清单、加载原数据快照，并按原策略、参数、成本和随机种子重跑，
最后比较输入指纹和结果哈希。缺少本地快照时会明确失败，不会静默换用新数据。

## 11. 复现边界

- 相同代码版本、数据快照、参数、成本和随机种子才能保证完全复现。
- 工作区有未提交修改时，代码版本会标记为 `-dirty`。
- 长期复现必须同时备份 `experiments/` 和 `data/snapshots/`。
- 数据源更新历史数据不会改写旧快照，重新下载会产生新的数据版本。
