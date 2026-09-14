"""broker_adapter 测试（python3 broker_adapter/test_broker.py）

分两部分：
  A. 单元测试（mock SDK，不联网、不需要 QMT 也能跑）：
     接口调用参数、.env 配置读取、错误码翻译、各类异常处理；
  B. 真实连接测试（模拟账户）：
     连接模拟账户、查询账户资金和持仓，绝不下单；
     需要 Windows + QMT 客户端已登录模拟账号 + .env 已配置，
     本机不具备条件时自动跳过（不算失败）。
"""
import os
import sys
from types import ModuleType
from unittest import mock

# 保证直接运行本文件时能找到项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker_adapter.base_broker import (BaseBroker, BrokerConfigError,
                                        BrokerConnectionError, BrokerDataError,
                                        BrokerOrderError,
                                        BrokerOrderUnknownError)
from broker_adapter.qmt_adapter import (QmtBroker, describe_error_code,
                                        load_broker_config, to_xt_code)
from broker_adapter.models import OrderStatus


# ---------- 假 SDK（注入 sys.modules，模拟 Windows 上的 xtquant） ----------

class FakeStockAccount:
    def __init__(self, account_id, account_type="STOCK"):
        self.account_id = account_id
        self.account_type = account_type


class FakeAsset:
    def __init__(self, **kw):
        self.account_id = kw.get("account_id", "88880000")
        self.total_asset = kw.get("total_asset", 1_000_000.0)
        self.cash = kw.get("cash", 500_000.0)
        self.frozen_cash = kw.get("frozen_cash", 0.0)
        self.market_value = kw.get("market_value", 500_000.0)
        self.fetch_balance = kw.get("fetch_balance", 500_000.0)


class FakePosition:
    def __init__(self, **kw):
        self.stock_code = kw.get("stock_code", "600519.SH")
        self.instrument_name = kw.get("instrument_name", "贵州茅台")
        self.volume = kw.get("volume", 100)
        self.can_use_volume = kw.get("can_use_volume", 100)
        self.frozen_volume = kw.get("frozen_volume", 0)
        self.avg_price = kw.get("avg_price", 1450.0)
        self.last_price = kw.get("last_price", 1500.0)
        self.market_value = kw.get("market_value", 150_000.0)
        self.profit_rate = kw.get("profit_rate", 0.0345)


class FakeOrder:
    def __init__(self, order_id, status=50, **kw):
        self.order_id = order_id
        self.order_status = status
        self.account_id = kw.get("account_id", "88880000")
        self.stock_code = kw.get("stock_code", "600519.SH")
        self.instrument_name = kw.get("instrument_name", "贵州茅台")
        self.order_type = kw.get("order_type", 23)
        self.order_volume = kw.get("order_volume", 100)
        self.price_type = kw.get("price_type", 11)
        self.price = kw.get("price", 1500.0)
        self.traded_volume = kw.get("traded_volume", 0)
        self.traded_price = kw.get("traded_price", 0.0)
        self.order_sysid = kw.get("order_sysid", "")
        self.order_time = kw.get("order_time", 1700000000)
        self.status_msg = kw.get("status_msg", "")
        self.strategy_name = kw.get("strategy_name", "abacktest")
        self.order_remark = kw.get("order_remark", "")
        self.direction = kw.get("direction", 23)
        self.offset_flag = kw.get("offset_flag", 0)


class FakeTrade:
    def __init__(self, trade_id="T001", order_id=100001, **kw):
        self.traded_id = trade_id
        self.order_id = order_id
        self.account_id = kw.get("account_id", "88880000")
        self.stock_code = kw.get("stock_code", "600519.SH")
        self.order_type = kw.get("order_type", 23)
        self.traded_volume = kw.get("traded_volume", 100)
        self.traded_price = kw.get("traded_price", 1500.0)
        self.traded_amount = kw.get("traded_amount", 150000.0)
        self.traded_time = kw.get("traded_time", 1700000001)
        self.order_sysid = kw.get("order_sysid", "")
        self.direction = kw.get("direction", 23)
        self.offset_flag = kw.get("offset_flag", 0)
        self.order_remark = kw.get("order_remark", "")


class FakeTrader:
    """记录调用、可配置返回值的假 XtQuantTrader

    connect()/subscribe() 的返回值在实例创建时才确定，
    所以用类属性预设（next_*），测试在 connect 之前设置。
    """

    next_connect_result = (0, "connected")
    next_subscribe_result = (0, "subscribed")
    last = None  # 最近创建的实例，测试断言时用

    def __init__(self, path, session, callback=None):
        FakeTrader.last = self
        self.path = path
        self.session = session
        self.callback = callback
        self.started = False
        self.stopped = False
        self.connect_result = FakeTrader.next_connect_result
        self.subscribe_result = FakeTrader.next_subscribe_result
        self.subscribed_account = None
        self.orders = []            # 记录每次下单的参数
        self.order_returns = []     # 手动覆盖的下单返回值队列
        self.order_seq = 100000
        self.cancel_calls = []
        self.cancel_returns = []
        self.cancelable = []        # query_stock_orders 的返回值
        self.all_orders = []
        self.trades = []
        self.queried_cancelable_only = None
        self.asset = None
        self.positions = []

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def connect(self):
        return self.connect_result

    def subscribe(self, account):
        self.subscribed_account = account
        return self.subscribe_result

    def unsubscribe(self, account):
        pass

    def order_stock(self, account, stock_code, order_type, order_volume,
                    price_type, price, strategy_name="", order_remark=""):
        self.orders.append(dict(account=account, code=stock_code,
                                order_type=order_type, volume=order_volume,
                                price_type=price_type, price=price,
                                strategy_name=strategy_name,
                                order_remark=order_remark))
        if self.order_returns:
            return self.order_returns.pop(0)
        self.order_seq += 1
        self.all_orders.append(FakeOrder(
            self.order_seq,
            stock_code=stock_code,
            order_type=order_type,
            order_volume=order_volume,
            price_type=price_type,
            price=price,
            strategy_name=strategy_name,
            order_remark=order_remark,
            direction=order_type,
        ))
        return self.order_seq

    def cancel_order_stock(self, account, order_id):
        self.cancel_calls.append(order_id)
        if self.cancel_returns:
            return self.cancel_returns.pop(0)
        return 0

    def query_stock_orders(self, account, cancelable_only=False):
        self.queried_cancelable_only = cancelable_only
        return list(self.cancelable if cancelable_only else self.all_orders)

    def query_stock_order(self, account, order_id):
        for order in self.all_orders:
            if int(order.order_id) == int(order_id):
                return order
        return None

    def query_stock_trades(self, account):
        return list(self.trades)

    def query_stock_asset(self, account):
        return self.asset

    def query_stock_positions(self, account):
        return list(self.positions)


class FakeXtData:
    def __init__(self):
        self.subscribed = []

    def subscribe_quote(self, stock_code, period="1d", start_time="",
                        end_time="", count=0, callback=None):
        self.subscribed.append(dict(code=stock_code, period=period,
                                    callback=callback))


def _build_fake_sdk_modules():
    """构造假的 xtquant 包（内容由各测试自行填充/替换）"""
    pkg = ModuleType("xtquant")
    names = ["xttrader", "xttype", "xtconstant", "xtdata"]
    mods = {}
    for n in names:
        mod = ModuleType(f"xtquant.{n}")
        mods[n] = mod
        setattr(pkg, n, mod)
    mods["xttrader"].XtQuantTrader = FakeTrader
    mods["xttrader"].XtQuantTraderCallback = object
    mods["xttype"].StockAccount = FakeStockAccount
    mods["xtconstant"].STOCK_BUY = 23
    mods["xtconstant"].STOCK_SELL = 24
    mods["xtconstant"].FIX_PRICE = 11
    mods["xtconstant"].LATEST_PRICE = 5
    mods["xtconstant"].ORDER_UNREPORTED = 48
    mods["xtconstant"].ORDER_WAIT_REPORTING = 49
    mods["xtconstant"].ORDER_REPORTED = 50
    mods["xtconstant"].ORDER_REPORTED_CANCEL = 51
    mods["xtconstant"].ORDER_PARTSUCC_CANCEL = 52
    mods["xtconstant"].ORDER_PART_CANCEL = 53
    mods["xtconstant"].ORDER_CANCELED = 54
    mods["xtconstant"].ORDER_PART_SUCC = 55
    mods["xtconstant"].ORDER_SUCCEEDED = 56
    mods["xtconstant"].ORDER_JUNK = 57
    mods["xtconstant"].ORDER_UNKNOWN = 255
    mods["xtdata"].subscribe_quote = FakeXtData().subscribe_quote
    sys_modules = {"xtquant": pkg}
    sys_modules.update({f"xtquant.{n}": m for n, m in mods.items()})
    return sys_modules


FAKE_SDK = _build_fake_sdk_modules()


def _fake_xtdata():
    """每测一个全新的假行情模块（避免订阅记录互相污染）"""
    mod = FAKE_SDK["xtquant.xtdata"]
    fake = FakeXtData()
    mod.subscribe_quote = fake.subscribe_quote
    return fake


def _make_connected_broker(**kwargs):
    """用假 SDK 建一个已连接的 QmtBroker，返回 (broker, 真实创建的假 trader)"""
    with mock.patch.dict("sys.modules", FAKE_SDK):
        b = QmtBroker(userdata_path="D:\\qmt\\userdata_mini",
                      account_id="88880000", **kwargs)
        b.connect()
    return b, b._trader


# ---------- A. 单元测试 ----------

def test_config_from_env():
    env = {"QMT_USERDATA_PATH": "D:\\国金QMT\\userdata_mini",
           "QMT_ACCOUNT": "66001234",
           "QMT_ACCOUNT_TYPE": "STOCK",
           "QMT_SESSION_ID": "42"}
    with mock.patch.dict(os.environ, env, clear=True), \
            mock.patch("broker_adapter.qmt_adapter.load_dotenv"):  # 不读真实 .env 文件
        cfg = load_broker_config()
        assert cfg["userdata_path"] == "D:\\国金QMT\\userdata_mini", cfg
        assert cfg["account_id"] == "66001234"
        assert cfg["session_id"] == 42
        b = QmtBroker()  # 全部走 .env
        assert b.userdata_path == "D:\\国金QMT\\userdata_mini"
        assert b.account_id == "66001234"
        assert b.session_id == 42
    print("✓ .env 配置读取：账号/客户端路径/会话 ID 全部来自环境变量")


def test_config_missing():
    with mock.patch.dict(os.environ, {}, clear=True), \
            mock.patch("broker_adapter.qmt_adapter.load_dotenv"):
        try:
            QmtBroker()
            raise AssertionError("缺配置时应抛 BrokerConfigError")
        except BrokerConfigError as e:
            assert "QMT_USERDATA_PATH" in str(e), e
    with mock.patch.dict(os.environ, {"QMT_USERDATA_PATH": "D:\\x\\userdata_mini"},
                         clear=True), \
            mock.patch("broker_adapter.qmt_adapter.load_dotenv"):
        try:
            QmtBroker()
            raise AssertionError("缺账号时应抛 BrokerConfigError")
        except BrokerConfigError as e:
            assert "QMT_ACCOUNT" in str(e), e
    print("✓ 缺配置友好报错：提示该在 .env 里填哪一项")


def test_to_xt_code():
    assert to_xt_code("600519") == "600519.SH"
    assert to_xt_code("000001") == "000001.SZ"
    assert to_xt_code("300750") == "300750.SZ"
    assert to_xt_code("688981") == "688981.SH"
    assert to_xt_code("830799") == "830799.BJ"
    assert to_xt_code("430047") == "430047.BJ"
    assert to_xt_code("600519.SH") == "600519.SH"  # 带后缀直接通过
    try:
        to_xt_code("abc")
        raise AssertionError("非法代码应报错")
    except BrokerConfigError:
        pass
    print("✓ 代码格式转换：6 位数字 → 沪/深/北交所后缀")


def test_connect_flow():
    b, t = _make_connected_broker()
    assert b._connected and t.started
    assert t.subscribed_account.account_id == "88880000"
    b.disconnect()
    assert not b._connected and t.stopped
    b.disconnect()  # 重复断开不抛异常
    print("✓ connect/disconnect：启动→连接→订阅账号→断开，重复断开安全")


def test_connect_failure():
    FakeTrader.next_connect_result = (-1, "连接被拒绝")
    try:
        with mock.patch.dict("sys.modules", FAKE_SDK):
            b = QmtBroker(userdata_path="D:\\qmt\\userdata_mini", account_id="88880000")
            try:
                b.connect()
                raise AssertionError("连接失败应抛 BrokerConnectionError")
            except BrokerConnectionError as e:
                assert e.error_code == -1 and "连接被拒绝" in str(e), e
            assert not b._connected
    finally:
        FakeTrader.next_connect_result = (0, "connected")
    print("✓ 连接失败：抛出带错误码的 BrokerConnectionError")


def test_subscribe_failure():
    FakeTrader.next_subscribe_result = (1, "账号未登录")
    try:
        with mock.patch.dict("sys.modules", FAKE_SDK):
            b = QmtBroker(userdata_path="D:\\qmt\\userdata_mini", account_id="88880000")
            try:
                b.connect()
                raise AssertionError("订阅失败应抛 BrokerConnectionError")
            except BrokerConnectionError as e:
                assert "88880000" in str(e), e
    finally:
        FakeTrader.next_subscribe_result = (0, "subscribed")
    print("✓ 账号订阅失败：提示确认账号在 QMT 客户端已登录")


def test_order_buy_sell():
    b, t = _make_connected_broker()
    oid = b.order_buy("600519", 1500.0, 100)
    assert oid == 100001
    o = t.orders[-1]
    assert o["code"] == "600519.SH" and o["order_type"] == 23
    assert o["volume"] == 100 and o["price_type"] == 11 and o["price"] == 1500.0
    b.order_sell("000001", 12.5, 300)
    assert t.orders[-1]["order_type"] == 24 and t.orders[-1]["code"] == "000001.SZ"
    b.order_sell("600519", 1500.0, 150)  # 卖出允许零股
    # 异常：未连接 / 整手 / 价格数量非法
    b2 = QmtBroker(userdata_path="D:\\qmt\\userdata_mini", account_id="88880000")
    for fn, args in ((b2.order_buy, ("600519", 1500.0, 100)),):
        try:
            fn(*args)
            raise AssertionError("未连接下单应抛 BrokerConnectionError")
        except BrokerConnectionError:
            pass
    for args in (("600519", 0, 100), ("600519", 1500.0, 0),
                 ("600519", -1, 100), ("600519", 1500.0, 150)):
        try:
            b.order_buy(*args)
            raise AssertionError(f"非法参数应报错：{args}")
        except BrokerOrderError:
            pass
    print("✓ 下单：买卖方向/代码后缀/整手校验/未连接报错 全部正确")


def test_order_sdk_failure():
    for bad in (-1, None):
        b, t = _make_connected_broker()
        t.order_returns = [bad]
        try:
            b.order_buy("600519", 1500.0, 100)
            raise AssertionError(f"SDK 返回 {bad} 应抛 BrokerOrderError")
        except BrokerOrderError as e:
            assert "on_order_error" in str(e), e
            assert isinstance(e, BrokerOrderUnknownError)
    print("✓ 下单失败（SDK 返回 -1/None）：标准异常 + 指向回调日志")


def test_error_code_messages():
    assert "资金余额不足" in describe_error_code(3)
    assert "未登录" in describe_error_code(1)
    assert describe_error_code(0) == "成功"
    assert "未知错误码 999" in describe_error_code(999)
    print("✓ 错误码翻译：常见错误码有中文说明，未知码有通用提示")


def test_cancel_order_all():
    b, t = _make_connected_broker()
    t.cancelable = [FakeOrder(7), FakeOrder(9)]
    assert b.cancel_order_all() == 2
    assert t.cancel_calls == [7, 9]
    assert t.queried_cancelable_only is True  # 只查可撤委托
    # 部分失败：失败的记日志，成功的继续撤
    t.cancelable = [FakeOrder(11), FakeOrder(12)]
    t.cancel_returns = [-1, 0]
    assert b.cancel_order_all() == 1
    # 没有可撤委托
    t.cancelable = []
    assert b.cancel_order_all() == 0
    print("✓ 一键撤单：只查可撤委托，失败不中断其余撤单")


def test_single_cancel():
    b, t = _make_connected_broker()
    t.all_orders = [FakeOrder(7)]
    assert b.cancel_order(7) is True
    assert t.cancel_calls[-1] == 7
    t.cancel_returns = [-1]
    try:
        b.cancel_order(8)
        raise AssertionError("单笔撤单失败应抛 BrokerOrderError")
    except BrokerOrderError as e:
        assert e.order_id == 8
    print("✓ 单笔撤单：成功返回 True，失败抛标准异常")


def test_order_and_trade_queries():
    b, t = _make_connected_broker()
    order = FakeOrder(
        101,
        status=55,
        order_remark="client_order_id=test-intent-1|buy",
        traded_volume=50,
        traded_price=10.5,
    )
    t.all_orders = [order]
    t.trades = [FakeTrade(
        trade_id="TRADE-1",
        order_id=101,
        traded_volume=50,
        traded_price=10.5,
        order_remark="client_order_id=test-intent-1|buy",
    )]
    snapshot = b.get_order(101)
    assert snapshot.broker_order_id == "101"
    assert snapshot.symbol == "600519"
    assert snapshot.status is OrderStatus.PARTIALLY_FILLED
    assert snapshot.client_order_id == "test-intent-1"
    assert snapshot.remaining_quantity == 50
    assert b.get_orders(symbol="600519")[0].broker_order_id == "101"
    trades = b.get_trades(order_id=101)
    assert len(trades) == 1 and trades[0].trade_id == "TRADE-1"
    detail = b.get_order_detail(101)
    assert detail.order.filled_quantity == 50
    assert detail.trades[0].amount == 150000.0
    print("✓ 订单/成交查询：统一字段、过滤和订单详情正确")


def test_get_account_info():
    b, t = _make_connected_broker()
    t.asset = FakeAsset(total_asset=1_234_567.89, cash=234_567.89)
    info = b.get_account_info()
    assert info["总资产"] == 1_234_567.89
    assert info["可用资金"] == 234_567.89
    assert set(info) == {"资金账号", "总资产", "可用资金", "冻结资金", "持仓市值", "可取资金"}
    t.asset = None
    try:
        b.get_account_info()
        raise AssertionError("查不到资产应抛 BrokerDataError")
    except BrokerDataError:
        pass
    print("✓ 账户资金查询：中文键字典 + 查不到时报标准异常")


def test_get_positions():
    b, t = _make_connected_broker()
    t.positions = [FakePosition(), FakePosition(stock_code="000001.SZ",
                                                instrument_name="平安银行", volume=500)]
    ps = b.get_positions()
    assert len(ps) == 2
    assert ps[0]["股票代码"] == "600519.SH" and ps[0]["股票名称"] == "贵州茅台"
    assert ps[1]["持仓数量"] == 500
    t.positions = []
    assert b.get_positions() == []  # 空仓返回空列表
    print("✓ 持仓查询：中文键列表 + 空仓返回空列表")


def test_subscribe_realtime():
    b, t = _make_connected_broker()
    got = []
    fake = _fake_xtdata()
    b.subscribe_realtime(["600519", "000001"], callback=lambda code, data: got.append(code))
    assert [s["code"] for s in fake.subscribed] == ["600519.SH", "000001.SZ"]
    assert all(s["period"] == "tick" for s in fake.subscribed)
    # 模拟一笔行情推送
    fake.subscribed[0]["callback"]({"lastPrice": 1500.5})
    assert got == ["600519.SH"]
    try:
        b.subscribe_realtime([], callback=None)
        raise AssertionError("空代码列表应报错")
    except BrokerDataError:
        pass
    print("✓ 实时行情订阅：代码转后缀 + tick 回调 + 空列表报错")


def test_reconnect_and_callbacks():
    events = []
    b, t = _make_connected_broker(
        on_event=lambda name, data: events.append((name, data))
    )
    callback = t.callback
    callback.on_stock_order(FakeOrder(
        201, status=50, order_type=23, direction=0
    ))
    callback.on_stock_trade(FakeTrade(trade_id="T201", order_id=201))
    assert events[-2][0] == "order"
    assert events[-2][1]["order"]["broker_order_id"] == "201"
    assert events[-2][1]["order"]["side"] == "buy"
    assert events[-1][0] == "trade"
    assert events[-1][1]["trade"]["trade_id"] == "T201"
    callback.on_disconnected()
    assert b._connected is False
    b.reconnect(max_attempts=1, delay=0)
    assert b._connected is True
    b.disconnect()
    print("✓ 断线回调与重连：状态复位，订单/成交回调字段完整")


def test_sdk_missing_friendly_error():
    # 未注入假 SDK 时：本机若能直接导入真实 xtquant 则跳过，否则验证友好报错
    try:
        from xtquant import xttrader  # noqa: F401
        print("跳过 SDK 缺失分支：本机可直接导入真实 xtquant")
        return
    except ImportError:
        pass
    with mock.patch.dict("sys.modules", {k: None for k in list(sys.modules)
                                         if k.startswith("xtquant")}):
        b = QmtBroker(userdata_path="D:\\qmt\\userdata_mini", account_id="88880000")
        try:
            b.connect()
            raise AssertionError("SDK 不可用应抛 BrokerConnectionError")
        except BrokerConnectionError as e:
            assert "xtquant" in str(e), e
    print("✓ SDK 缺失/不可用：友好提示安装方法与平台限制")


def test_interface_completeness():
    # 核心接口都定义在抽象基类里，QmtBroker 全部实现
    expected = {"connect", "disconnect", "order_buy", "order_sell",
                "submit_order", "cancel_order", "cancel_order_all",
                "get_order", "get_orders", "get_trades", "get_order_detail",
                "get_positions", "get_account_info", "subscribe_realtime",
                "reconnect"}
    abstract = set()
    for name in expected:
        if getattr(BaseBroker, name, None) is None:
            abstract.add(name)
    assert not abstract
    for name in expected:
        assert callable(getattr(QmtBroker, name, None)), f"QmtBroker 缺少接口 {name}"
    print("✓ 接口契约：订单/成交/撤单/查询/重连接口全部实现")


# ---------- B. 真实连接测试（模拟账户，绝不下单） ----------

def test_real_sim_account():
    """连接模拟账户，查询账户资金和持仓，不做任何下单/撤单。

    运行条件（缺一即跳过，不算失败）：
      1. Windows 系统（QMT 客户端是 Windows 软件）
      2. 已安装可用 xtquant SDK
      3. QMT 客户端已启动并登录模拟账号
      4. .env 已配置 QMT_USERDATA_PATH / QMT_ACCOUNT
    """
    if sys.platform != "win32":
        print(f"跳过真实连接测试：QMT 客户端仅支持 Windows（本机是 {sys.platform}）")
        return
    try:
        from xtquant import xttrader  # noqa: F401
    except ImportError:
        print("跳过真实连接测试：本机未安装可用的 xtquant SDK")
        return
    try:
        broker = QmtBroker()
    except BrokerConfigError as e:
        print(f"跳过真实连接测试：{e}")
        return
    # 下面连接失败会抛 BrokerConnectionError → 测试失败（说明环境/配置有问题，应修）
    broker.connect()
    try:
        info = broker.get_account_info()
        positions = broker.get_positions()
    finally:
        broker.disconnect()
    print(f"✓ 模拟账户连接成功：总资产 {info['总资产']:,.2f} 元，"
          f"可用资金 {info['可用资金']:,.2f} 元，持仓 {len(positions)} 只")
    assert info["总资产"] >= 0
    assert isinstance(positions, list)


def run_test():
    print("===== broker_adapter 测试 =====")
    for fn in (test_config_from_env, test_config_missing, test_to_xt_code,
               test_connect_flow, test_connect_failure, test_subscribe_failure,
               test_order_buy_sell, test_order_sdk_failure,
               test_error_code_messages, test_cancel_order_all,
               test_single_cancel, test_order_and_trade_queries,
               test_get_account_info, test_get_positions,
               test_subscribe_realtime, test_sdk_missing_friendly_error,
               test_reconnect_and_callbacks,
               test_interface_completeness, test_real_sim_account):
        fn()
    print("===== 全部测试通过 =====")


if __name__ == "__main__":
    run_test()
