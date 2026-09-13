"""
A股行情数据获取模块（日线 + 分钟线）

基于 AKShare 获取 A 股行情数据（前复权），并支持本地缓存：
- 相同请求（股票代码 + 起止日期）只下载一次，之后直接读本地缓存
- 缓存文件保存在项目根目录的 data/cache/ 目录下

数据源（自动切换，一个失败自动换下一个）：
日线：
1. 腾讯行情（ak.stock_zh_a_hist_tx）—— 主数据源
2. 东方财富（ak.stock_zh_a_hist）—— 备用数据源
分钟线：
1. 新浪行情（ak.stock_zh_a_minute）—— 只提供最近约 5 个交易日

统一入口 load_market_data(code, start, end, freq)：
freq 可选 daily / 1min / 5min / 15min / 30min / 60min，
同一份数据可以无缝切换到回测、参数寻优、批量回测。

运行测试：
    python utils/data_loader.py
"""

import datetime
import os
import re
import time

import pandas as pd

try:
    import akshare as ak
except ImportError:  # akshare 未安装时延迟报错，给出更友好的提示
    ak = None

# ---- 常量 ----

# 需要的列：AKShare 中文列名 -> 标准英文列名（东方财富源用）
_COLUMN_MAP = {
    "日期": "date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",
}

# 标准输出列
_STANDARD_COLUMNS = ["date", "open", "high", "low", "close", "volume"]

# 缓存目录：项目根目录/data/cache/
# （打包成 exe 后项目目录是临时解压目录，可用环境变量 ABACKTEST_CACHE_DIR 指定缓存位置）
_CACHE_DIR = os.environ.get("ABACKTEST_CACHE_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cache"
)
CACHE_DIR = _CACHE_DIR  # 公开别名，供界面「打开缓存目录」使用

# 网络失败时的重试间隔（秒）
_RETRY_INTERVAL = 2


# ---- 参数校验 ----

def _check_env():
    """确认 akshare 已安装"""
    if ak is None:
        raise ImportError("未安装 akshare，请先运行：pip install -r requirements.txt")


def _validate_code(code) -> str:
    """校验并规范化股票代码：必须是 6 位数字"""
    if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code.strip()):
        raise ValueError(f"股票代码格式不正确：{code!r}（应为 6 位数字，如 600519）")
    return code.strip()


def _validate_date(d, name: str) -> str:
    """把日期统一转成 AKShare 需要的 YYYYMMDD 字符串

    支持三种输入：datetime.date / "20240101" / "2024-01-01"（也接受 "2024-1-1"）
    """
    if isinstance(d, datetime.datetime):
        d = d.date()
    if isinstance(d, datetime.date):
        return d.strftime("%Y%m%d")
    if isinstance(d, str):
        d = d.strip()
        if re.fullmatch(r"\d{8}", d):
            try:
                return datetime.datetime.strptime(d, "%Y%m%d").strftime("%Y%m%d")
            except ValueError:
                pass
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", d):
            try:
                return datetime.datetime.strptime(d, "%Y-%m-%d").strftime("%Y%m%d")
            except ValueError:
                pass
    raise ValueError(
        f"{name} 日期格式不正确：{d!r}（支持 20240101、2024-01-01 或 date 对象）"
    )


def _with_market_prefix(code: str) -> str:
    """6 位代码 -> 带市场前缀的代码（腾讯/新浪接口需要），如 600519 -> sh600519"""
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    raise ValueError(f"暂不支持该股票代码：{code}（本软件仅支持 A 股）")


# ---- 本地缓存 ----

def _cache_path(code: str, start: str, end: str) -> str:
    """缓存文件路径，如 data/cache/600519_20230101_20260101.csv"""
    return os.path.join(_CACHE_DIR, f"{code}_{start}_{end}.csv")


def _read_cache(path: str):
    """读取缓存；文件不存在或损坏时返回 None"""
    try:
        df = pd.read_csv(path)
        if list(df.columns) != _STANDARD_COLUMNS:
            return None  # 列名不对，视为缓存损坏
        df["date"] = pd.to_datetime(df["date"])
        return df
    except Exception:
        return None


def _save_cache(df: pd.DataFrame, path: str):
    """保存缓存"""
    os.makedirs(_CACHE_DIR, exist_ok=True)
    df.to_csv(path, index=False)


# ---- 数据源 ----

def _fetch_tencent(code: str, start_s: str, end_s: str) -> pd.DataFrame:
    """数据源1：腾讯行情。volume 单位是股，返回标准 6 列。"""
    df = ak.stock_zh_a_hist_tx(
        symbol=_with_market_prefix(code),
        start_date=start_s,
        end_date=end_s,
        adjust="qfq",  # 前复权
    )
    return df[_STANDARD_COLUMNS].copy()


def _fetch_eastmoney(code: str, start_s: str, end_s: str) -> pd.DataFrame:
    """数据源2：东方财富。中文列名，volume 单位是手，需换算成股。"""
    df = ak.stock_zh_a_hist(
        symbol=code,
        period="daily",
        start_date=start_s,
        end_date=end_s,
        adjust="qfq",  # 前复权
    )
    df = df.rename(columns=_COLUMN_MAP)[_STANDARD_COLUMNS].copy()
    df["volume"] = df["volume"] * 100  # 手 -> 股
    return df


# 数据源列表：(名称, 获取函数, 重试次数)
_SOURCES = [
    ("腾讯", _fetch_tencent, 2),
    ("东方财富", _fetch_eastmoney, 1),
]


# ---- 主函数 ----

def load_daily_data(code: str, start, end, use_cache: bool = True) -> pd.DataFrame:
    """获取 A 股日线数据（前复权）

    参数:
        code:      股票代码，如 "600519"
        start:     开始日期（含），支持 "20240101" / "2024-01-01" / datetime.date
        end:       结束日期（含），格式同上
        use_cache: 是否使用本地缓存，默认 True

    返回:
        标准 DataFrame，列：date, open, high, low, close, volume
        - date:   交易日期（按升序排列）
        - volume: 成交量，单位：股

    说明:
        - 相同请求只下载一次，之后读本地缓存（data/cache/ 目录）
        - 腾讯为主数据源，失败自动切换东方财富
    """
    _check_env()

    # ---- 1. 参数校验 ----
    code = _validate_code(code)
    _with_market_prefix(code)  # 提前确认是支持的 A 股代码段
    start_s = _validate_date(start, "start")
    end_s = _validate_date(end, "end")
    if start_s > end_s:
        raise ValueError(f"开始日期({start_s})不能晚于结束日期({end_s})")

    cache_file = _cache_path(code, start_s, end_s)

    # ---- 2. 先读本地缓存 ----
    if use_cache and os.path.exists(cache_file):
        df = _read_cache(cache_file)
        if df is not None:
            print(f"[data_loader] 使用本地缓存：data/cache/{os.path.basename(cache_file)}")
            return df
        print("[data_loader] 缓存文件损坏，重新下载...")

    # ---- 3. 逐个数据源尝试下载 ----
    df_raw = None
    errors = []
    for source_name, fetch, retries in _SOURCES:
        for attempt in range(1, retries + 1):
            try:
                print(f"[data_loader] 正在从{source_name}下载 {code} {start_s}~{end_s} 的日线数据...")
                df_raw = fetch(code, start_s, end_s)
                print(f"[data_loader] 数据源：{source_name} ✓")
                break
            except Exception as e:
                errors.append(f"{source_name}：{e}")
                if attempt < retries:
                    print(f"[data_loader] 第 {attempt} 次下载失败，{_RETRY_INTERVAL} 秒后重试...")
                    time.sleep(_RETRY_INTERVAL)
        if df_raw is not None:
            break
        print(f"[data_loader] {source_name} 数据源不可用，切换下一个数据源...")

    if df_raw is None:
        raise RuntimeError(
            "所有数据源均下载失败：\n" + "\n".join(errors) + "\n请检查网络后重试。"
        )

    # ---- 4. 校验与整理 ----
    if df_raw.empty:
        raise ValueError(
            f"没有获取到 {code} 在 {start_s}~{end_s} 的数据，"
            "请检查股票代码是否正确、日期区间是否在上市之后。"
        )
    if list(df_raw.columns) != _STANDARD_COLUMNS:
        raise ValueError(f"数据列不符合标准：{list(df_raw.columns)}，数据接口可能有变动")

    df = df_raw.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df = df.dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0).astype("int64")

    # ---- 5. 保存缓存 ----
    if use_cache:
        _save_cache(df, cache_file)
        print(f"[data_loader] 已缓存到：data/cache/{os.path.basename(cache_file)}")

    return df


# ---- 分钟线数据 ----

# 支持的分钟周期
_MINUTE_PERIODS = ("1", "5", "15", "30", "60")


def _validate_period(period) -> str:
    """校验分钟周期：支持 "1" / "5" / "15" / "30" / "60"（分钟）"""
    p = str(period).strip().lower().replace("min", "")
    if p not in _MINUTE_PERIODS:
        raise ValueError(f"不支持的分钟周期：{period!r}，可选：{'/'.join(_MINUTE_PERIODS)} 分钟")
    return p


def _fetch_sina_minute(code: str, period: str) -> pd.DataFrame:
    """数据源：新浪分钟线（ak.stock_zh_a_minute），返回标准 6 列

    注意：新浪分钟线只保留最近约 5 个交易日；尾部的收盘集合竞价行
    只有成交量没有价格（open/high/low/close 为 NaN），整理时过滤掉。
    """
    df = ak.stock_zh_a_minute(symbol=_with_market_prefix(code),
                              period=period, adjust="qfq")
    if "day" in df.columns:
        df = df.rename(columns={"day": "date"})
    if "date" not in df.columns:
        raise ValueError(f"分钟线数据格式异常，缺少日期列：{list(df.columns)}")
    cols = [c for c in _STANDARD_COLUMNS if c in df.columns]
    return df[cols].copy()


# 分钟线数据源列表：(名称, 获取函数, 重试次数)
_MINUTE_SOURCES = [("新浪", _fetch_sina_minute, 2)]


def load_minute_data(code: str, period: str = "1", start=None, end=None,
                     use_cache: bool = True) -> pd.DataFrame:
    """获取 A 股分钟线数据（前复权，新浪源）

    参数:
        code:      股票代码，如 "600519"
        period:    分钟周期："1" / "5" / "15" / "30" / "60"（分钟）
        start/end: 可选过滤区间（新浪只返回最近约 5 个交易日，
                   早于该范围的数据取不到，过滤后可能变少）
        use_cache: 是否使用本地缓存。分钟线盘中会更新，缓存按
                   “下载当天”命名，隔天自动重新下载

    返回:
        标准 DataFrame：date（精确到分钟）, open, high, low, close, volume（股）
    """
    _check_env()
    code = _validate_code(code)
    _with_market_prefix(code)  # 提前确认是支持的 A 股代码段
    period = _validate_period(period)
    start_s = _validate_date(start, "start") if start is not None else None
    end_s = _validate_date(end, "end") if end is not None else None
    if start_s and end_s and start_s > end_s:
        raise ValueError(f"开始日期({start_s})不能晚于结束日期({end_s})")

    today = datetime.date.today().strftime("%Y%m%d")
    cache_file = os.path.join(_CACHE_DIR, f"minute_{code}_{period}min_{today}.csv")

    # ---- 1. 先读本地缓存（当天下载的才有效）----
    df = None
    if use_cache and os.path.exists(cache_file):
        df = _read_cache(cache_file)
        if df is not None:
            print(f"[data_loader] 使用本地缓存（今日下载）：data/cache/{os.path.basename(cache_file)}")
        else:
            print("[data_loader] 分钟线缓存文件损坏，重新下载...")

    if df is None:
        if use_cache:
            # 清理同一股票同周期的旧缓存（隔天已失效）
            try:
                for old in os.listdir(_CACHE_DIR):
                    if old.startswith(f"minute_{code}_{period}min_") \
                            and old != os.path.basename(cache_file):
                        os.remove(os.path.join(_CACHE_DIR, old))
            except OSError:
                pass

        # ---- 2. 逐个数据源尝试下载 ----
        df_raw, errors = None, []
        for source_name, fetch, retries in _MINUTE_SOURCES:
            for attempt in range(1, retries + 1):
                try:
                    print(f"[data_loader] 正在从{source_name}下载 {code} {period}分钟线数据...")
                    df_raw = fetch(code, period)
                    print(f"[data_loader] 数据源：{source_name} ✓")
                    break
                except Exception as e:
                    errors.append(f"{source_name}：{e}")
                    if attempt < retries:
                        print(f"[data_loader] 第 {attempt} 次下载失败，{_RETRY_INTERVAL} 秒后重试...")
                        time.sleep(_RETRY_INTERVAL)
            if df_raw is not None:
                break
            print(f"[data_loader] {source_name} 数据源不可用，切换下一个数据源...")
        if df_raw is None:
            raise RuntimeError(
                "分钟线数据下载失败：\n" + "\n".join(errors) +
                "\n提示：新浪分钟线接口偶尔不稳定，请稍后重试。"
            )

        # ---- 3. 校验与整理 ----
        df = df_raw.copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df.dropna(subset=["open", "high", "low", "close"])  # 过滤收盘集合竞价空行
        df = df.sort_values("date").reset_index(drop=True)
        df["volume"] = df["volume"].fillna(0).astype("int64")
        if df.empty:
            raise ValueError(f"没有获取到 {code} 的 {period} 分钟线数据，请稍后重试。")
        if use_cache:
            _save_cache(df, cache_file)
            print(f"[data_loader] 已缓存到：data/cache/{os.path.basename(cache_file)}")

    # ---- 4. 按起止日期过滤（含 start 当天、含 end 当天）----
    if start_s is not None:
        df = df[df["date"] >= pd.to_datetime(start_s, format="%Y%m%d")]
    if end_s is not None:
        df = df[df["date"] < pd.to_datetime(end_s, format="%Y%m%d") + pd.Timedelta(days=1)]
    if df.empty:
        raise ValueError(
            f"{code} 的 {period} 分钟线在指定区间内没有数据。"
            f"提示：新浪分钟线只提供最近约 5 个交易日，请把区间改到最近几天。")
    return df.reset_index(drop=True)


def load_market_data(code: str, start, end, freq: str = "daily", **kwargs) -> pd.DataFrame:
    """统一行情入口：按频率加载数据（日线 / 分钟线）

    参数:
        freq: "daily"（日线，默认）/ "1min" / "5min" / "15min" / "30min" / "60min"
        start/end: 起止日期（分钟线只保留最近约 5 个交易日，自动截取交集）
        其余参数透传给 load_daily_data / load_minute_data

    返回:
        标准 DataFrame（date/open/high/low/close/volume），date 为 datetime
    """
    freq = str(freq).strip().lower()
    if freq in ("daily", "day", "d", "1d", "日线", "日"):
        return load_daily_data(code, start, end, **kwargs)
    if freq.endswith("min"):
        return load_minute_data(code, period=freq[:-3], start=start, end=end, **kwargs)
    raise ValueError(f"不支持的行情频率：{freq!r}，"
                     f"可选：daily / 1min / 5min / 15min / 30min / 60min")


# ---- 测试函数 ----

def run_test():
    """测试：拉取贵州茅台(600519)近 3 年日线数据并校验格式

    运行方式：
        python utils/data_loader.py
    """
    print("===== 测试开始：拉取贵州茅台(600519)近 3 年日线数据 =====")
    print()

    end = datetime.date.today()
    start = end - datetime.timedelta(days=365 * 3)

    df = load_daily_data("600519", start, end)

    # ---- 校验格式 ----
    assert list(df.columns) == _STANDARD_COLUMNS, f"列名不正确：{list(df.columns)}"
    assert len(df) > 500, f"数据条数偏少：{len(df)}（近 3 年应有约 700 个交易日）"
    assert df["date"].is_monotonic_increasing, "日期未按升序排列"
    assert (df["high"] >= df["low"]).all(), "存在最高价低于最低价的异常行"
    assert df[["open", "high", "low", "close"]].notna().all().all(), "价格存在空值"

    # ---- 展示结果 ----
    print(f"✓ 校验通过，共 {len(df)} 条数据")
    print(f"  日期范围：{df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}")
    print(f"  最新收盘价：{df['close'].iloc[-1]:.2f} 元")
    print()
    print("前 5 行：")
    print(df.head().to_string(index=False))
    print()
    print("后 5 行：")
    print(df.tail().to_string(index=False))
    # ---- 分钟线数据：拉取 600519 的 1 分钟数据并校验 ----
    print()
    print("===== 测试开始：拉取贵州茅台(600519) 1 分钟线数据 =====")
    df_min = load_minute_data("600519", period="1")
    assert list(df_min.columns) == _STANDARD_COLUMNS, f"列名不正确：{list(df_min.columns)}"
    assert len(df_min) > 50, f"分钟数据条数偏少：{len(df_min)}"
    assert df_min["date"].is_monotonic_increasing, "时间未按升序排列"
    assert (df_min["high"] >= df_min["low"]).all(), "存在最高价低于最低价的异常行"
    assert df_min[["open", "high", "low", "close"]].notna().all().all(), "价格存在空值"
    print(f"✓ 校验通过，共 {len(df_min)} 根 1 分钟K线")
    print(f"  时间范围：{df_min['date'].iloc[0]} ~ {df_min['date'].iloc[-1]}")

    # ---- 统一入口：频率分发 + 非法频率报错 ----
    d1 = load_market_data("600519", start, end, freq="daily")
    assert len(d1) == len(df), "daily 频率应等价于 load_daily_data"
    m1 = load_market_data("600519", start, end, freq="5min")
    assert len(m1) > 10, "5min 频率应有数据"
    try:
        load_market_data("600519", start, end, freq="2min")
    except ValueError as e:
        print(f"  ✓ 非法频率报错：{e}")
    else:
        raise AssertionError("非法频率应该抛出 ValueError")
    assert load_market_data("600519", start, end, freq="1min").equals(df_min), \
        "1min 频率应等价于 load_minute_data"

    print()
    print("===== 测试通过 =====")
    return df


if __name__ == "__main__":
    run_test()
