"""参数优化共用组件：参数范围解析、单组合评估、多进程并行

被 optimization/grid_search.py 和 optimization/genetic_algo.py 共用，不直接对外使用。

Windows 兼容说明:
    多进程用 ProcessPoolExecutor（Windows/macOS 默认 spawn 启动方式），
    因此评估函数 _evaluate_one 必须是模块顶层函数（可被 pickle）。
"""

import logging
import math
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed

# 保证直接运行本目录下文件（python optimization/grid_search.py）时能找到 core 等包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.backtest_engine import BacktestEngine
from core.risk_analysis import analyze_portfolio
from strategies.factory import create_strategy

logger = logging.getLogger("optimization")

# 优化目标可选指标（每个组合回测后都计算全部这些指标）
METRIC_COLUMNS = [
    "累计收益率", "年化收益率", "年化波动率", "夏普比率", "卡玛比率",
    "最大回撤", "胜率", "盈亏比", "总交易次数", "总手续费", "期末总资产", "基准收益率",
]

# 越小越好的指标（其余默认越大越好）
MINIMIZE_METRICS = {"最大回撤", "年化波动率", "总手续费"}

# 指标别名（方便书写）
_METRIC_ALIASES = {
    "夏普": "夏普比率", "sharpe": "夏普比率",
    "年化收益": "年化收益率",
    "累计收益": "累计收益率",
    "回撤": "最大回撤",
    "卡玛": "卡玛比率",
    "波动率": "年化波动率",
    "手续费": "总手续费",
}


def normalize_metric(metric: str) -> str:
    """指标别名转标准名；未知指标报错"""
    name = _METRIC_ALIASES.get(metric, metric)
    if name not in METRIC_COLUMNS:
        raise ValueError(f"未知优化指标：{metric!r}，可选：{METRIC_COLUMNS}")
    return name


def direction(metric: str) -> float:
    """优化方向：越大越好 -> 1.0；越小越好（回撤/波动/手续费） -> -1.0"""
    return -1.0 if normalize_metric(metric) in MINIMIZE_METRICS else 1.0


def safe_float(x):
    """转 float；None / NaN / inf 一律转 None（保证可导出 JSON）"""
    if x is None:
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def parse_ranges(param_ranges: dict) -> dict:
    """把各种参数范围写法统一成候选值列表 {key: [v1, v2, ...]}

    四种写法:
        range(5, 20)                -> [5, 6, ..., 19]
        [5, 10, 20]                 -> 候选值列表，原样使用
        (5, 20, 5)                  -> 元组 = 最小值/最大值/步长
        {"min": 5, "max": 20, "step": 5}

    返回:
        {key: 候选值列表}；整数范围保持 int 类型，非法范围抛 ValueError
    """
    if not param_ranges:
        raise ValueError("param_ranges 不能为空")

    out = {}
    for key, r in param_ranges.items():
        if isinstance(r, range):
            out[key] = list(r)
        elif isinstance(r, list):
            out[key] = list(r)  # 列表 = 候选值
        elif isinstance(r, tuple) and len(r) == 3 and all(isinstance(v, (int, float)) for v in r):
            lo, hi, step = r  # 三元组 = 最小/最大/步长
        elif isinstance(r, dict):
            lo, hi, step = r.get("min"), r.get("max"), r.get("step")
            if None in (lo, hi, step):
                raise ValueError(f"参数 {key} 的字典范围需要 min/max/step 三个键：{r!r}")
        else:
            raise ValueError(f"参数 {key} 的范围写法不认识：{r!r}")

        if key not in out:
            if not isinstance(step, (int, float)) or step <= 0 or lo > hi:
                raise ValueError(f"参数 {key} 的范围不合法：{r!r}")
            vals = []
            v = lo
            while v <= hi + 1e-9:
                vals.append(round(v, 6))
                v += step
            if isinstance(lo, int) and isinstance(hi, int) and isinstance(step, int):
                out[key] = [int(x) for x in vals]  # 整数参数保持 int
            else:
                out[key] = vals

        if not out[key]:
            raise ValueError(f"参数 {key} 的候选值为空：{r!r}")
    return out


def build_engine_kwargs(init_cash, commission, slippage, code, rf,
                        t_plus_1, price_limit, stamp_tax, transfer_fee):
    """组装回测引擎的全局参数字典（网格与遗传共用）"""
    return {
        "init_cash": init_cash, "commission": commission, "slippage": slippage,
        "code": code, "rf": rf,
        "t_plus_1": t_plus_1, "price_limit": price_limit,
        "stamp_tax": stamp_tax, "transfer_fee": transfer_fee,
    }


def _evaluate_one(task):
    """单个参数组合的回测评估（模块顶层函数，多进程 worker 直接调用）

    参数:
        task: (参数 dict, 行情 DataFrame, 策略名, 引擎参数字典)

    返回:
        (是否成功, 结果行 dict 或 错误信息 str, 参数 dict)
    """
    params, df, strategy_name, engine_kwargs = task
    try:
        strategy = create_strategy(strategy_name, **params)
        entries, exits = strategy.generate_signals(df)
        if entries.sum() == 0:
            raise ValueError("策略没有产生任何买入信号")
        bt = BacktestEngine(df, entries, exits, **engine_kwargs).run()
        analyzer = analyze_portfolio(bt.portfolio, rf=engine_kwargs.get("rf", 0.0))
        m = bt.get_metrics()
        rm = analyzer.compute_metrics()
        row = {
            **params,
            "累计收益率": m["累计收益率"],
            "年化收益率": m["年化收益率"],
            "年化波动率": rm["年化波动率"],
            "夏普比率": m["夏普比率"],
            "卡玛比率": rm["卡玛比率"],
            "最大回撤": m["最大回撤"],
            "胜率": m["胜率"],
            "盈亏比": rm["盈亏比"],
            "总交易次数": m["总交易次数"],
            "总手续费": m["总手续费"],
            "期末总资产": m["期末总资产"],
            "基准收益率": m["基准收益率"],
        }
        return (True, row, params)
    except Exception as e:
        return (False, f"{type(e).__name__}: {e}", params)


def run_tasks(tasks, max_workers=None, progress_cb=None):
    """并发（或串行）评估一批组合

    参数:
        tasks:       任务列表（元素格式见 _evaluate_one）
        max_workers: 进程数；None 自动（min(任务数, CPU 核数)），1 表示串行
        progress_cb: 进度回调 progress_cb(已完成数, 总数)，供界面进度条使用

    返回:
        (有效结果行列表, 失败列表 [(参数 dict, 错误信息), ...])
    """
    if not tasks:
        return [], []

    def _report(done):
        if progress_cb:
            try:
                progress_cb(done, len(tasks))
            except Exception:
                pass  # 进度回调出错不影响主流程

    if max_workers == 1:  # 串行（任务少或调试时用，避免进程启动开销）
        rows, failed = [], []
        for t in tasks:
            ok, row, params = _evaluate_one(t)
            (rows if ok else failed).append(row if ok else (params, row))
            _report(len(rows) + len(failed))
        return rows, failed
    workers = max_workers or min(len(tasks), os.cpu_count() or 1)
    rows, failed = [], []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_evaluate_one, t) for t in tasks]
        for fut in as_completed(futures):
            ok, row, params = fut.result()
            (rows if ok else failed).append(row if ok else (params, row))
            _report(len(rows) + len(failed))
    return rows, failed
