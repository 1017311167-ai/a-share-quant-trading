"""代码版本与运行环境元数据。"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def get_code_version(project_root=None) -> str:
    """返回 Git 提交短哈希；工作区有已跟踪修改时追加 -dirty。"""
    configured = os.environ.get("ABACKTEST_CODE_VERSION")
    if configured:
        return configured
    root = Path(project_root or PROJECT_ROOT)
    try:
        revision = _git(root, "rev-parse", "--short=12", "HEAD")
        if not revision:
            return "unknown"
        dirty = _git(root, "status", "--porcelain", "--untracked-files=no")
        return f"{revision}-dirty" if dirty else revision
    except OSError:
        return "unknown"


def get_environment_info() -> dict:
    """返回影响实验复现的运行环境信息。"""
    packages = {}
    for name in ("pandas", "numpy", "numba", "vectorbt", "akshare"):
        try:
            module = __import__(name)
            packages[name] = getattr(module, "__version__", "unknown")
        except Exception as exc:
            packages[name] = f"unavailable:{type(exc).__name__}"
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "packages": packages,
        "code_version": get_code_version(),
    }


def stable_hash(data, length: int = 20) -> str:
    """对可 JSON 序列化数据计算稳定哈希。"""
    payload = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def _git(root: Path, *args) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()
