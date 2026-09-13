"""策略与参数的统一元数据。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from utils.versioning import stable_hash


@dataclass(frozen=True)
class ParameterSpec:
    """单个策略参数的类型、默认值和约束。"""

    name: str
    default: Any
    description: str
    kind: str = "auto"
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: bool = False
    exclusive_maximum: bool = False
    choices: tuple[Any, ...] = ()
    step: float | None = None
    optimize: bool = True

    def __post_init__(self):
        if not self.name:
            raise ValueError("参数名不能为空")
        kind = self.kind
        if kind == "auto":
            if isinstance(self.default, bool):
                kind = "bool"
            elif isinstance(self.default, int):
                kind = "int"
            elif isinstance(self.default, float):
                kind = "float"
            elif isinstance(self.default, str):
                kind = "str"
            else:
                kind = type(self.default).__name__
        if kind not in {"int", "float", "bool", "str"}:
            raise ValueError(f"不支持的参数类型：{kind!r}")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "choices", tuple(self.choices))
        self.validate(self.default)

    def validate(self, value) -> Any:
        """校验并规范化一个参数值。"""
        if self.kind == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{self.name} 必须是布尔值：{value!r}")
            return value
        if self.kind == "int":
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{self.name} 必须是整数：{value!r}")
            result = value
        elif self.kind == "float":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{self.name} 必须是数字：{value!r}")
            result = float(value)
        elif self.kind == "str":
            if not isinstance(value, str):
                raise ValueError(f"{self.name} 必须是字符串：{value!r}")
            result = value
        else:
            raise ValueError(f"不支持的参数类型：{self.kind!r}")
        if self.minimum is not None:
            invalid = result <= self.minimum if self.exclusive_minimum \
                else result < self.minimum
            if invalid:
                relation = "大于" if self.exclusive_minimum else "不小于"
                raise ValueError(f"{self.name} 必须{relation} {self.minimum}：{result!r}")
        if self.maximum is not None:
            invalid = result >= self.maximum if self.exclusive_maximum \
                else result > self.maximum
            if invalid:
                relation = "小于" if self.exclusive_maximum else "不大于"
                raise ValueError(f"{self.name} 必须{relation} {self.maximum}：{result!r}")
        if self.choices and result not in self.choices:
            raise ValueError(f"{self.name} 只支持 {self.choices}：{result!r}")
        return result

    def to_dict(self) -> dict:
        data = asdict(self)
        data["choices"] = list(self.choices)
        return data


@dataclass(frozen=True)
class StrategyMetadata:
    """策略的稳定身份和参数 Schema。"""

    key: str
    name: str
    version: str
    description: str
    parameters: tuple[ParameterSpec, ...]
    required_columns: tuple[str, ...]
    signal_schema_version: str = "signals-v1"
    tags: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    implementation_hash: str = "unknown"

    def __post_init__(self):
        if not self.key or not self.name or not self.version:
            raise ValueError("策略 key、name 和 version 不能为空")
        names = [item.name for item in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError(f"策略 {self.key} 存在重复参数名：{names}")

    @property
    def default_params(self) -> dict:
        return {item.name: item.default for item in self.parameters}

    @property
    def params_help(self) -> dict:
        return {item.name: item.description for item in self.parameters}

    @property
    def parameter_schema(self) -> list[dict]:
        return [item.to_dict() for item in self.parameters]

    @property
    def metadata_hash(self) -> str:
        return stable_hash(self.to_dict(include_hash=False))

    def parameter_map(self) -> dict[str, ParameterSpec]:
        return {item.name: item for item in self.parameters}

    def validate_params(self, params: dict) -> dict:
        specs = self.parameter_map()
        unknown = sorted(set(params) - set(specs))
        if unknown:
            raise ValueError(f"策略 {self.key} 不支持参数：{unknown}")
        merged = dict(self.default_params)
        for name, value in params.items():
            merged[name] = specs[name].validate(value)
        return merged

    def to_dict(self, *, include_hash: bool = True) -> dict:
        data = {
            "key": self.key,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "parameter_schema": self.parameter_schema,
            "required_columns": list(self.required_columns),
            "signal_schema_version": self.signal_schema_version,
            "tags": list(self.tags),
            "constraints": list(self.constraints),
            "implementation_hash": self.implementation_hash,
        }
        if include_hash:
            data["metadata_hash"] = self.metadata_hash
        return data


def parameter_hash(params: dict, length: int = 16) -> str:
    return stable_hash(params, length=length)
