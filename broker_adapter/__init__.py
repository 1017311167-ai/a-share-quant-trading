"""券商对接层：统一交易接口 + QMT（xtquant）适配器 + MockBroker

上层代码只依赖 BaseBroker，换券商只需新增适配器：

    from broker_adapter import BaseBroker, QmtBroker, BrokerError
"""
from broker_adapter.base_broker import (BaseBroker, BrokerConfigError,
                                        BrokerConnectionError, BrokerDataError,
                                        BrokerError, BrokerOrderError,
                                        BrokerOrderUnknownError)
from broker_adapter.qmt_adapter import QmtBroker, load_broker_config, to_xt_code
from broker_adapter.mock_broker import MockBroker
from broker_adapter.models import (
    BrokerEvent,
    OrderDetail,
    OrderRequest,
    OrderSide,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    TradeSnapshot,
)
from broker_adapter.contract import assert_broker_contract, assert_fill_contract

__all__ = [
    "BaseBroker", "QmtBroker", "MockBroker",
    "BrokerError", "BrokerConfigError", "BrokerConnectionError",
    "BrokerOrderError", "BrokerDataError",
    "BrokerOrderUnknownError",
    "BrokerEvent", "OrderDetail", "OrderRequest", "OrderSide",
    "OrderSnapshot", "OrderStatus", "OrderType", "TradeSnapshot",
    "assert_broker_contract", "assert_fill_contract",
    "load_broker_config", "to_xt_code",
]
