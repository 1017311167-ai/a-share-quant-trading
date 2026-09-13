"""
A股交易规则配置

集中存放交易费用、涨跌停、T+1 等规则参数，
方便统一调整，回测引擎会读取这里。
"""

# ---- 交易费用（默认值，可在界面中修改）----
COMMISSION_RATE = 0.00025    # 佣金：万 2.5（买卖各收一次）
COMMISSION_MIN = 5.0         # 佣金最低收费：5 元
STAMP_TAX_RATE = 0.0005      # 印花税：卖出时收取 0.05%
TRANSFER_FEE_RATE = 0.00001  # 过户费：0.001%（买卖各收一次）

# ---- A股特有交易规则 ----
T_PLUS_1 = True              # T+1：当天买入的股票，第二天才能卖出

# 涨跌停幅度（按板块区分）
PRICE_LIMIT = {
    "main": 0.10,            # 沪深主板：±10%
    "gem": 0.20,             # 创业板(300开头)/科创板(688开头)：±20%
    "st": 0.05,              # ST 股：±5%
    "bse": 0.30,             # 北交所：±30%
}


def get_price_limit(code: str) -> float:
    """根据股票代码判断涨跌停幅度"""
    if code.startswith(("300", "301", "688", "689")):
        return PRICE_LIMIT["gem"]
    if code.startswith(("8", "4")):
        return PRICE_LIMIT["bse"]
    # TODO: 识别 ST 股（股票名称含 "ST"）
    return PRICE_LIMIT["main"]
