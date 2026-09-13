"""QMT 券商适配器：封装迅投 QMT（miniQMT）xtquant SDK

用法:
    from broker_adapter.qmt_adapter import QmtBroker
    broker = QmtBroker()            # 账号、客户端路径全部从 .env 读取
    broker.connect()                # 连接 QMT 客户端并订阅资金账号
    info = broker.get_account_info()
    positions = broker.get_positions()
    order_id = broker.order_buy("600519", 1500.0, 100)
    broker.disconnect()

配置（项目根目录 .env，复制 .env.example 填写，禁止硬编码在代码里）:
    QMT_USERDATA_PATH    QMT 客户端安装目录下的 userdata 文件夹路径
                         （如 D:\\迅投极速交易终端\\userdata_mini）
    QMT_ACCOUNT          资金账号（模拟账户填模拟账号）
    QMT_ACCOUNT_TYPE     账号类型，默认 STOCK（股票）
    QMT_SESSION_ID       会话 ID（可选，不填自动生成，同账号多程序时需区分）

说明:
    - xtquant 只在用到时才导入，未安装也不影响主程序运行（连接时报友好错误）；
    - QMT 客户端是 Windows 软件，xtquant 的交易功能只能在 Windows 上使用；
    - 模拟账户与实盘用法完全一致，只是 .env 里填模拟账号。
"""
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from broker_adapter.base_broker import (BaseBroker, BrokerConfigError,
                                        BrokerConnectionError, BrokerDataError,
                                        BrokerOrderError)

logger = logging.getLogger(__name__)

# 项目根目录的 .env（开发环境）；打包成 exe 后还会读 exe 同目录的 .env
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------- 配置读取（禁止硬编码账号/路径，全部来自 .env） ----------

def _load_env():
    """加载 .env（项目根目录 + 当前目录，已存在的环境变量不会被覆盖）"""
    load_dotenv(_PROJECT_ROOT / ".env")
    load_dotenv(Path.cwd() / ".env")


def _env_int(key, default=None):
    v = os.getenv(key)
    try:
        return int(v) if v is not None and v.strip() else default
    except ValueError:
        return default


def load_broker_config() -> dict:
    """从 .env 读取 QMT 配置，返回 dict（缺什么值就是空串，由调用方校验）"""
    _load_env()
    return {
        "userdata_path": os.getenv("QMT_USERDATA_PATH", "").strip(),
        "account_id": os.getenv("QMT_ACCOUNT", "").strip(),
        "account_type": os.getenv("QMT_ACCOUNT_TYPE", "STOCK").strip() or "STOCK",
        "session_id": _env_int("QMT_SESSION_ID", None),
    }


# ---------- 常见错误码 → 中文说明 ----------

# QMT 官方文档常见错误码（不同券商可能略有差异，未知码走通用提示）
ERROR_CODE_MESSAGES = {
    0: "成功",
    1: "用户未登录",
    3: "资金余额不足",
    4: "持仓数量不足",
    5: "参数错误",
    6: "系统内部错误",
    7: "委托处理中",
    8: "非交易时段",
    9: "账户状态异常",
    10: "撤单失败",
    11: "委托编号不存在",
    12: "委托不在可撤状态",
    13: "委托已撤销",
    14: "委托已成交",
    15: "委托已废单",
    16: "交易密码错误",
    17: "无交易权限",
    18: "委托数量不是整手（100 股的整数倍）",
    19: "委托数量超过上限",
    20: "委托价格非法",
    21: "委托价格超出涨跌停范围",
    22: "委托已过期",
}


def describe_error_code(code) -> str:
    """把错误码翻译成中文说明；未知码给通用提示"""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return f"未知错误码：{code!r}"
    return ERROR_CODE_MESSAGES.get(code, f"未知错误码 {code}，请查看 QMT 客户端日志")


# ---------- 工具函数 ----------

def to_xt_code(code: str) -> str:
    """6 位数字代码 → 带交易所后缀的 xtquant 格式（如 600519 → 600519.SH）"""
    code = str(code).strip()
    if "." in code:
        return code.upper()
    if len(code) != 6 or not code.isdigit():
        raise BrokerConfigError(f"股票代码格式不正确：{code!r}（应为 6 位数字，如 600519）")
    if code.startswith(("5", "6", "9")):
        return f"{code}.SH"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return f"{code}.SZ"


class _TraderCallback:
    """连接 xtquant 回调（XtQuantTraderCallback 子类）。

    作用：把 SDK 的异步事件（下单失败、连接断开等）翻译成日志，
    可选把事件转给用户回调 on_event(event_name, data_dict)。
    基类在 _connect 时动态绑定真实 XtQuantTraderCallback。
    """

    def __init__(self, on_event=None):
        self._on_event = on_event

    def _emit(self, name, data):
        if self._on_event:
            try:
                self._on_event(name, data)
            except Exception:  # 用户回调出错不影响交易
                logger.exception("券商事件回调处理出错")

    # ---- 以下方法会在 _connect 时被挂到真实基类签名上 ----

    def on_disconnected(self):
        logger.warning("[QMT] 连接已断开，请检查客户端或网络")
        self._emit("disconnected", {})

    def on_order_error(self, order_error):
        logger.error("[QMT] 下单失败：%s", _format_order_error(order_error))
        self._emit("order_error", _format_order_error(order_error))

    def on_cancel_error(self, cancel_error):
        logger.error("[QMT] 撤单失败：%s", _format_cancel_error(cancel_error))
        self._emit("cancel_error", _format_cancel_error(cancel_error))


def _format_order_error(e) -> dict:
    """XtOrderError 对象 → 中文 dict"""
    try:
        return {"订单号": e.order_id, "错误码": e.error_id,
                "说明": describe_error_code(e.error_id), "原始信息": e.error_msg}
    except AttributeError:
        return {"原始信息": str(e)}


def _format_cancel_error(e) -> dict:
    try:
        return {"订单号": e.order_id, "错误码": e.error_id,
                "说明": describe_error_code(e.error_id), "原始信息": e.error_msg}
    except AttributeError:
        return {"原始信息": str(e)}


class QmtBroker(BaseBroker):
    """QMT 交易适配器（miniQMT，xtquant SDK）。

    实现 broker_adapter.base_broker.BaseBroker 的全部接口。
    账号、客户端路径从 .env 读取；也可以显式传参覆盖（便于测试）。
    """

    def __init__(self, userdata_path=None, account_id=None, session_id=None,
                 account_type=None, on_event=None):
        cfg = load_broker_config()
        self.userdata_path = userdata_path or cfg["userdata_path"]
        self.account_id = account_id or cfg["account_id"]
        self.session_id = session_id if session_id is not None \
            else (cfg["session_id"] or int(time.time() * 1000) % 10_000_000)
        self.account_type = account_type or cfg["account_type"]
        self.on_event = on_event
        if not self.userdata_path:
            raise BrokerConfigError(
                "未配置 QMT 客户端路径：请在 .env 里填写 QMT_USERDATA_PATH"
                "（QMT 客户端安装目录下的 userdata 文件夹，如 D:\\迅投极速交易终端\\userdata_mini）")
        if not self.account_id:
            raise BrokerConfigError(
                "未配置 QMT 资金账号：请在 .env 里填写 QMT_ACCOUNT（模拟账户填模拟账号）")
        self._connected = False
        self._trader = None
        self._account = None
        self._sdk = None  # {"xttrader":.., "xttype":.., "xtconstant":.., "xtdata":..}
        self._subscribed_codes = set()

    # ---------- SDK 延迟导入 ----------

    def _import_sdk(self):
        """用到时才导入 xtquant；未安装/平台不支持给友好错误"""
        if self._sdk is not None:
            return self._sdk
        try:
            from xtquant import xtconstant, xtdata, xttrader, xttype
        except ImportError as e:
            raise BrokerConnectionError(
                f"未安装或无法加载 xtquant SDK：{e}。"
                "请先 pip install xtquant，并在 Windows 上安装 QMT 客户端（交易功能仅支持 Windows）") from e
        self._sdk = {"xttrader": xttrader, "xttype": xttype,
                     "xtconstant": xtconstant, "xtdata": xtdata}
        return self._sdk

    # ---------- 连接管理 ----------

    def connect(self):
        """连接 QMT 客户端并订阅资金账号"""
        if self._connected:
            return
        sdk = self._import_sdk()
        try:
            self._trader = sdk["xttrader"].XtQuantTrader(
                self.userdata_path, self.session_id,
                callback=self._make_sdk_callback(sdk))
            self._trader.start()
            result = self._trader.connect()
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerConnectionError(f"连接 QMT 客户端失败：{e}") from e
        code, msg = self._unwrap_connect_result(result)
        if code != 0:
            raise BrokerConnectionError(
                f"连接 QMT 客户端失败：{msg}（{describe_error_code(code)}）",
                error_code=code)
        self._account = sdk["xttype"].StockAccount(self.account_id, self.account_type)
        try:
            result = self._trader.subscribe(self._account)
        except Exception as e:
            raise BrokerConnectionError(f"订阅资金账号失败：{e}") from e
        code, msg = self._unwrap_connect_result(result)
        if code != 0:
            raise BrokerConnectionError(
                f"订阅资金账号 {self.account_id} 失败：{msg}"
                f"（{describe_error_code(code)}），请确认账号在 QMT 客户端中已登录",
                error_code=code)
        self._connected = True
        logger.info("[QMT] 已连接客户端并订阅资金账号 %s", self.account_id)

    def _make_sdk_callback(self, sdk):
        """把 _TraderCallback 的事件处理方法绑到 SDK 回调基类上。

        动态创建子类而不是静态继承：SDK 未安装时本模块也能正常导入。
        """
        inner = _TraderCallback(self.on_event)
        base = sdk["xttrader"].XtQuantTraderCallback

        class Callback(base):
            def on_disconnected(self):
                inner.on_disconnected()

            def on_order_error(self, order_error):
                inner.on_order_error(order_error)

            def on_cancel_error(self, cancel_error):
                inner.on_cancel_error(cancel_error)

            def on_stock_order(self, order):
                inner._emit("order", {"订单号": getattr(order, "order_id", None),
                                      "状态": getattr(order, "order_status", None)})

            def on_stock_trade(self, trade):
                inner._emit("trade", {"成交编号": getattr(trade, "traded_id", None),
                                      "成交价": getattr(trade, "traded_price", None)})

        return Callback()

    @staticmethod
    def _unwrap_connect_result(result):
        """connect/subscribe 的返回值可能是 (code, msg) 元组、dict 或 None"""
        if result is None:
            return -1, "SDK 无返回结果"
        if isinstance(result, (tuple, list)) and len(result) >= 2:
            try:
                return int(result[0]), str(result[1] or "")
            except (TypeError, ValueError):
                return -1, str(result)
        if isinstance(result, dict):
            code = int(result.get("code", result.get("error_code", -1)))
            return code, str(result.get("message", result.get("msg", "")))
        return -1, str(result)

    def disconnect(self):
        """断开连接（重复调用安全，失败只记日志不抛异常）"""
        try:
            if self._trader is not None and self._connected:
                self._trader.unsubscribe(self._account)
                self._trader.stop()
                logger.info("[QMT] 已断开连接")
        except Exception as e:
            logger.warning("[QMT] 断开连接时出错（可忽略）：%s", e)
        finally:
            self._connected = False

    def _ensure_connected(self):
        if not self._connected:
            raise BrokerConnectionError("尚未连接 QMT 客户端，请先调用 connect()")

    # ---------- 下单 / 撤单 ----------

    def _place_order(self, code, price, volume, order_type, side, require_lot=True):
        self._ensure_connected()
        try:
            price = float(price)
            volume = int(volume)
        except (TypeError, ValueError):
            raise BrokerOrderError(f"委托参数格式不正确：价格 {price!r} / 数量 {volume!r}")
        if price <= 0:
            raise BrokerOrderError(f"委托价格必须大于 0：{price}")
        if volume <= 0:
            raise BrokerOrderError(f"委托数量必须大于 0：{volume}")
        if require_lot and volume % 100 != 0:
            raise BrokerOrderError(f"A 股买入必须是 100 股的整数倍：{volume} 股")
        sdk = self._sdk
        xt_code = to_xt_code(code)
        try:
            order_id = self._trader.order_stock(
                self._account, xt_code, order_type, volume,
                sdk["xtconstant"].FIX_PRICE, price,
                "abacktest", f"{side}{volume}股@{price}")
        except Exception as e:
            raise BrokerOrderError(f"{side}委托 {code} 失败：{e}") from e
        if order_id is None or int(order_id) < 0:
            raise BrokerOrderError(
                f"{side}委托 {code} 失败（SDK 返回 {order_id}）。"
                "异步错误详情会通过 on_order_error 回调返回，请查看日志")
        logger.info("[QMT] %s委托已报：%s %s 股 @ %.2f 元，订单号 %s",
                    side, code, volume, price, order_id)
        return int(order_id)

    def order_buy(self, code, price, volume):
        """限价买入（A 股买入必须 100 股整数倍），返回订单号"""
        self._ensure_connected()  # 先检查连接，未连接报标准异常而不是 SDK 空指针
        return self._place_order(code, price, volume,
                                 self._sdk["xtconstant"].STOCK_BUY, "买入")

    def order_sell(self, code, price, volume):
        """限价卖出（卖出允许零股），返回订单号"""
        self._ensure_connected()
        return self._place_order(code, price, volume,
                                 self._sdk["xtconstant"].STOCK_SELL, "卖出",
                                 require_lot=False)

    def cancel_order_all(self):
        """撤销当前账户所有可撤委托，返回实际发出的撤单笔数"""
        self._ensure_connected()
        try:
            orders = self._trader.query_stock_orders(self._account, cancelable_only=True)
        except Exception as e:
            raise BrokerDataError(f"查询可撤委托失败：{e}") from e
        orders = orders or []
        if not orders:
            logger.info("[QMT] 当前没有可撤委托")
            return 0
        ok = 0
        for order in orders:
            try:
                ret = self._trader.cancel_order_stock(self._account, order.order_id)
            except Exception as e:
                logger.warning("[QMT] 撤单 %s 出错：%s", getattr(order, "order_id", "?"), e)
                continue
            if int(ret) == 0:
                ok += 1
                logger.info("[QMT] 撤单已报：订单号 %s", order.order_id)
            else:
                logger.warning("[QMT] 撤单失败：订单号 %s，返回 %s（%s）",
                               order.order_id, ret, describe_error_code(ret))
        return ok

    # ---------- 查询 ----------

    def get_positions(self):
        """查询持仓，返回中文键 dict 列表（空仓返回 []）"""
        self._ensure_connected()
        try:
            positions = self._trader.query_stock_positions(self._account) or []
        except Exception as e:
            raise BrokerDataError(f"查询持仓失败：{e}") from e
        return [{
            "股票代码": getattr(p, "stock_code", ""),
            "股票名称": getattr(p, "instrument_name", ""),
            "持仓数量": getattr(p, "volume", 0),
            "可用数量": getattr(p, "can_use_volume", 0),
            "冻结数量": getattr(p, "frozen_volume", 0),
            "成本价": getattr(p, "avg_price", 0.0),
            "最新价": getattr(p, "last_price", 0.0),
            "市值": getattr(p, "market_value", 0.0),
            "浮动盈亏比例": getattr(p, "profit_rate", 0.0),
        } for p in positions]

    def get_account_info(self):
        """查询账户资金，返回中文键 dict"""
        self._ensure_connected()
        try:
            asset = self._trader.query_stock_asset(self._account)
        except Exception as e:
            raise BrokerDataError(f"查询账户资金失败：{e}") from e
        if asset is None:
            raise BrokerDataError("未查询到账户资金信息，请确认资金账号已订阅且已登录")
        return {
            "资金账号": getattr(asset, "account_id", self.account_id),
            "总资产": getattr(asset, "total_asset", 0.0),
            "可用资金": getattr(asset, "cash", 0.0),
            "冻结资金": getattr(asset, "frozen_cash", 0.0),
            "持仓市值": getattr(asset, "market_value", 0.0),
            "可取资金": getattr(asset, "fetch_balance", 0.0),
        }

    # ---------- 实时行情 ----------

    def subscribe_realtime(self, codes, callback=None):
        """订阅 tick 实时行情。

        callback 签名为 callback(code, tick_dict)；
        注意：行情推送需要进程持续运行（如主程序循环或 trader.run_forever()）。
        """
        sdk = self._sdk or self._import_sdk()
        codes = [codes] if isinstance(codes, str) else list(codes)
        if not codes:
            raise BrokerDataError("订阅实时行情至少需要 1 个股票代码")

        def make_cb(code):
            def cb(data):
                if callback:
                    callback(code, data)
                else:
                    logger.debug("[QMT] 行情 %s：%s", code, data)
            return cb

        xtdata = sdk["xtdata"]
        for code in codes:
            xt_code = to_xt_code(code)
            try:
                xtdata.subscribe_quote(xt_code, period="tick",
                                       callback=make_cb(xt_code))
                self._subscribed_codes.add(xt_code)
            except Exception as e:
                raise BrokerDataError(f"订阅 {xt_code} 实时行情失败：{e}") from e
        logger.info("[QMT] 已订阅 %d 只股票实时行情：%s",
                    len(self._subscribed_codes), "、".join(sorted(self._subscribed_codes)))
