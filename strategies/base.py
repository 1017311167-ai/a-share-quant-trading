"""策略基类 —— 所有策略的统一接口"""


class Strategy:
    """策略基类

    写新策略时，继承这个类并实现以下内容即可，回测引擎会自动调用：
    - name:          策略显示名称
    - PARAMS_HELP:   参数说明字典（参数名 -> 说明文字），供界面展示
    - default_params: 类方法，返回默认参数字典
    - generate_signals: 核心方法，根据行情生成买卖信号
    """

    name: str = "未命名策略"
    PARAMS_HELP: dict = {}

    @classmethod
    def default_params(cls) -> dict:
        """返回默认参数字典（新建策略时请重写）"""
        raise NotImplementedError("子类必须实现 default_params 方法")

    def generate_signals(self, df):
        """根据行情数据生成买卖信号

        参数:
            df: 行情数据，含 open/high/low/close/volume 列

        返回:
            (entries, exits)：
                entries: 买入信号（True 表示当天买入）
                exits:   卖出信号（True 表示当天卖出）
                两者都是与 df 等长的布尔 Series，索引为日期
        """
        raise NotImplementedError("子类必须实现 generate_signals 方法")
