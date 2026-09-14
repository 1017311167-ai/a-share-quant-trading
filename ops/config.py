"""按 profile/account 隔离运行配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from ops.redaction import redact


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_ROOT = PROJECT_ROOT / "configs"


@dataclass(frozen=True)
class ProfileConfig:
    profile: str
    account: str | None
    sources: tuple[str, ...]
    values: dict = field(default_factory=dict)

    def public_dict(self):
        return {
            "profile": self.profile,
            "account": self.account,
            "sources": list(self.sources),
            "values": redact(self.values, account=True),
        }


def load_profile(
        profile="paper",
        account=None,
        *,
        config_root=None,
        apply=True,
        environ=None,
) -> ProfileConfig:
    """加载 `configs/<profile>/common.env` 和账户覆盖文件。

    profile 和 account 均经过路径校验，禁止通过 `../` 跨目录读取配置。
    """
    profile = _safe_name(profile, "profile")
    account_name = _safe_name(account, "account") if account else None
    root = Path(config_root or DEFAULT_CONFIG_ROOT)
    profile_dir = root / profile
    if not profile_dir.exists():
        raise FileNotFoundError(f"配置 profile 不存在：{profile_dir}")
    sources = []
    values = {}
    for name in ("common.env", f"{account_name}.env" if account_name else None):
        if name is None:
            continue
        path = profile_dir / name
        if path.exists():
            sources.append(str(path))
            values.update({
                key: value
                for key, value in dotenv_values(path).items()
                if value is not None
            })
    target = os.environ if environ is None else environ
    if apply:
        target.update({str(key): str(value) for key, value in values.items()})
    return ProfileConfig(
        profile=profile,
        account=account_name,
        sources=tuple(sources),
        values=values,
    )


def _safe_name(value, label):
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} 不能为空")
    if not all(char.isalnum() or char in "-_." for char in text):
        raise ValueError(f"{label} 包含非法字符：{value!r}")
    if text in {".", ".."} or ".." in text:
        raise ValueError(f"{label} 不能包含路径跳转：{value!r}")
    return text
