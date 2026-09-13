"""券商对接层：统一交易接口 + QMT（xtquant）适配器

上层代码只依赖 BaseBroker 的 8 个接口，换券商只需新增适配器：

    from broker_adapter import BaseBroker, QmtBroker, BrokerError
"""
from broker_adapter.base_broker import (BaseBroker, BrokerConfigError,
                                        BrokerConnectionError, BrokerDataError,
                                        BrokerError, BrokerOrderError)
from broker_adapter.qmt_adapter import QmtBroker, load_broker_config, to_xt_code

__all__ = [
    "BaseBroker", "QmtBroker",
    "BrokerError", "BrokerConfigError", "BrokerConnectionError",
    "BrokerOrderError", "BrokerDataError",
    "load_broker_config", "to_xt_code",
]
