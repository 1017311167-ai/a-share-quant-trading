"""策略统一接口、元数据和标准信号输出。"""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

from strategies.metadata import ParameterSpec, StrategyMetadata, parameter_hash
from strategies.signals import build_signal_output


class Strategy:
    """所有策略的基础接口。

    子类必须定义 strategy_key、version、PARAMETER_SPECS 和 generate_signals。
    旧代码仍可使用 generate_signals() 返回的 (entries, exits)；新代码应优先
    使用 generate_signal_output() 获取版本、参数和信号哈希。
    """

    strategy_key: str = "base"
    name: str = "未命名策略"
    version: str = "1.0.0"
    description: str = ""
    required_columns: tuple[str, ...] = ("close",)
    signal_schema_version: str = "signals-v1"
    tags: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    PARAMETER_SPECS: tuple[ParameterSpec, ...] = ()
    PARAMS_HELP: dict = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        specs = tuple(cls.__dict__.get("PARAMETER_SPECS", ()))
        if not all(isinstance(item, ParameterSpec) for item in specs):
            raise TypeError(f"{cls.__name__}.PARAMETER_SPECS 必须由 ParameterSpec 组成")
        if specs:
            cls.PARAMS_HELP = {item.name: item.description for item in specs}

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            key=cls.strategy_key,
            name=cls.name,
            version=cls.version,
            description=cls.description,
            parameters=cls.PARAMETER_SPECS,
            required_columns=cls.required_columns,
            signal_schema_version=cls.signal_schema_version,
            tags=cls.tags,
            constraints=cls.constraints,
            implementation_hash=_implementation_hash(cls),
        )

    @classmethod
    def default_params(cls) -> dict:
        return cls.metadata().default_params

    @classmethod
    def validate_params(cls, params: dict) -> dict:
        return cls.metadata().validate_params(params)

    def params(self) -> dict:
        """返回当前实例的标准化参数。"""
        metadata = self.metadata()
        values = {}
        for spec in metadata.parameters:
            if not hasattr(self, spec.name):
                raise AttributeError(f"策略 {metadata.key} 缺少参数属性：{spec.name}")
            values[spec.name] = spec.validate(getattr(self, spec.name))
        return values

    @property
    def parameter_hash(self) -> str:
        return parameter_hash(self.params())

    def generate_signals(self, df):
        """计算原始买卖信号，保持旧接口兼容。"""
        raise NotImplementedError("子类必须实现 generate_signals 方法")

    def generate_signal_output(self, df):
        """计算并校验标准信号输出。"""
        entries, exits = self.generate_signals(df)
        return build_signal_output(self, df, entries, exits)


def _implementation_hash(strategy_cls) -> str:
    """对策略源文件计算实现哈希。"""
    try:
        source = inspect.getsourcefile(strategy_cls)
        if not source:
            return "unknown"
        content = Path(source).read_bytes()
        return hashlib.sha256(content).hexdigest()[:16]
    except (OSError, TypeError):
        return "unknown"
