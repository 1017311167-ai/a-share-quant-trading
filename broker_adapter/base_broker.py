"""券商对接基类：统一所有券商/交易终端的接口约定 + 标准化异常

任何券商适配器（QMT、Mock、同花顺、自研等）都继承 BaseBroker，
上层代码（策略、自动交易）只依赖本文件，换券商不用改一行上层代码。
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod

from broker_adapter.models import (
    OrderDetail,
    OrderRequest,
    OrderSide,
    OrderSnapshot,
)


# ---------- 标准化异常 ----------

class BrokerError(Exception):
    """所有券商异常的基类。message 一定是对用户友好的中文说明"""


class BrokerConfigError(BrokerError):
    """配置缺失/非法：.env 里没填账号、客户端路径等必要项"""


class BrokerConnectionError(BrokerError):
    """连接失败：客户端连不上、账号订阅失败、SDK 未安装等

    属性:
        error_code: 券商返回的错误码（int，没有则为 None）
    """

    def __init__(self, message, error_code=None):
        super().__init__(message)
        self.error_code = error_code


class BrokerOrderError(BrokerError):
    """下单/撤单失败：委托被拒绝、返回错误码等

    属性:
        order_id: 订单号（失败时一般为 None）
        error_code: 券商返回的错误码（int，没有则为 None）
    """

    def __init__(self, message, order_id=None, error_code=None):
        super().__init__(message)
        self.order_id = order_id
        self.error_code = error_code


class BrokerOrderUnknownError(BrokerOrderError):
    """下单请求已发出，但无法确认券商是否受理。"""


class BrokerDataError(BrokerError):
    """查询数据失败：查账户、持仓、实时行情出错"""


# ---------- 统一接口 ----------

class BaseBroker(ABC):
    """券商交易接口基类。

    约定：
      - 所有方法失败时抛上面定义的标准化异常，绝不返回错误码让调用方猜；
      - connect() 成功后才能调用下单/查询类方法，否则抛 BrokerConnectionError；
      - 股票代码统一用 6 位纯数字（如 "600519"），带交易所后缀的转换
        由适配器内部处理。
    """

    @abstractmethod
    def connect(self):
        """连接交易客户端并订阅资金账号（成功返回 None，失败抛 BrokerConnectionError）"""

    @abstractmethod
    def disconnect(self):
        """断开连接（重复调用安全，失败只记日志不抛异常）"""

    def reconnect(self, max_attempts: int = 3, delay: float = 1.0):
        """断开后重连，默认最多重试 3 次。"""
        if max_attempts < 1:
            raise ValueError("max_attempts 必须至少为 1")
        self.disconnect()
        last_error = None
        for attempt in range(1, max_attempts + 1):
            try:
                self.connect()
                return
            except BrokerConnectionError as exc:
                last_error = exc
                if attempt < max_attempts:
                    time.sleep(max(0.0, float(delay)))
        if last_error is not None:
            raise last_error

    @abstractmethod
    def order_buy(self, code: str, price: float, volume: int) -> int:
        """限价买入。

        参数:
            code:   股票代码（6 位数字，如 "600519"）
            price:  委托价格（元）
            volume: 委托数量（股）
        返回:
            订单号（int）。失败抛 BrokerOrderError。
        """

    @abstractmethod
    def order_sell(self, code: str, price: float, volume: int) -> int:
        """限价卖出。参数/返回同 order_buy"""

    def submit_order(self, request: OrderRequest) -> int:
        """统一提交订单请求并返回券商订单号。"""
        if request.side is OrderSide.BUY:
            return self.order_buy(request.symbol, request.price, request.quantity)
        return self.order_sell(request.symbol, request.price, request.quantity)

    @abstractmethod
    def cancel_order(self, order_id) -> bool:
        """撤销单笔委托，券商确认受理时返回 True。"""

    @abstractmethod
    def get_order(self, order_id) -> OrderSnapshot:
        """查询单笔订单详情。"""

    @abstractmethod
    def get_orders(self, *, cancelable_only: bool = False,
                   symbol: str | None = None, status=None,
                   start_time=None, end_time=None) -> list:
        """查询订单列表。"""

    @abstractmethod
    def get_trades(self, *, order_id=None, symbol: str | None = None,
                   start_time=None, end_time=None) -> list:
        """查询成交列表。"""

    def get_order_detail(self, order_id) -> OrderDetail:
        """组合订单和成交，返回完整订单详情。"""
        order = self.get_order(order_id)
        trades = self.get_trades(order_id=order_id)
        return OrderDetail(order=order, trades=tuple(trades))

    @abstractmethod
    def cancel_order_all(self) -> int:
        """撤销当前账户所有可撤委托。

        返回:
            实际发出的撤单笔数（int）。
        """

    @abstractmethod
    def get_positions(self) -> list:
        """查询持仓。

        返回:
            持仓列表，每项一个 dict，键（中文）：
            股票代码 / 股票名称 / 持仓数量 / 可用数量 / 冻结数量 /
            成本价 / 最新价 / 市值 / 浮动盈亏比例
        """

    @abstractmethod
    def get_account_info(self) -> dict:
        """查询账户资金。

        返回:
            dict，键（中文）：资金账号 / 总资产 / 可用资金 /
            冻结资金 / 持仓市值 / 可取资金
        """

    @abstractmethod
    def subscribe_realtime(self, codes, callback=None):
        """订阅实时行情（tick 推送）。

        参数:
            codes:    股票代码列表（6 位数字，如 ["600519", "000001"]）
            callback: 可选，每笔行情到达时回调（参数由适配器约定）
        注意:
            行情推送需要进程持续运行才能收到，具体见适配器说明。
        """
