"""生成可直接双击的 macOS .app 应用包。"""

from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import stat
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def build_app(output_dir=None, name="量化交易台"):
    output_dir = Path(output_dir or PROJECT_ROOT / "dist")
    output_dir.mkdir(parents=True, exist_ok=True)
    app = output_dir / f"{name}.app"
    contents = app / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    executable_name = "QuantTradingDesk"
    executable = macos / executable_name
    architecture = platform.machine()
    arch_prefix = (
        f'/usr/bin/arch -{architecture} '
        if architecture in {"arm64", "x86_64"} else ""
    )
    script = f"""#!/bin/zsh
set -e
export QUANT_PROJECT_ROOT="{PROJECT_ROOT}"
exec {arch_prefix}"{sys.executable}" "{PROJECT_ROOT / 'scripts/macos/launcher.py'}" --demo --app-mode
"""
    executable.write_text(script, encoding="utf-8")
    executable.chmod(
        executable.stat().st_mode
        | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    plist = {
        "CFBundleName": name,
        "CFBundleDisplayName": name,
        "CFBundleIdentifier": "com.local.quant-trading-desk",
        "CFBundleVersion": "0.2.0",
        "CFBundleShortVersionString": "0.2.0",
        "CFBundleExecutable": executable_name,
        "CFBundlePackageType": "APPL",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
    }
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(plist, handle)
    command = output_dir / "启动量化交易台.command"
    command.write_text(
        script,
        encoding="utf-8",
    )
    command.chmod(
        command.stat().st_mode
        | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return {
        "app": str(app),
        "command": str(command),
        "python": sys.executable,
        "project_root": str(PROJECT_ROOT),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成 macOS 应用")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--name", default="量化交易台")
    args = parser.parse_args(argv)
    result = build_app(args.output_dir, args.name)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
