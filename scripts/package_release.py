"""创建可追溯的源码和 Windows 部署发布包。"""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INCLUDE = (
    "app",
    "broker_adapter",
    "configs",
    "core",
    "data",
    "docs",
    "execution",
    "gui",
    "notification",
    "ops",
    "optimization",
    "persistence",
    "research",
    "risk",
    "scripts",
    "strategies",
    "trading",
    "utils",
    "main.py",
    "README.md",
    "requirements.txt",
    "requirements-ci.txt",
)
EXCLUDED_PARTS = {
    "__pycache__",
    ".git",
    "logs",
    "backups",
    "experiments",
    "snapshots",
    "cache",
    "dist",
    "build",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".db", ".sqlite", ".sqlite3"}


def build_release(version, output_dir=None):
    version = str(version).strip()
    if not version:
        raise ValueError("version 不能为空")
    output_dir = Path(output_dir or PROJECT_ROOT / "dist")
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"quant-trading-{version}.zip"
    files = []
    with zipfile.ZipFile(
        archive, "w", compression=zipfile.ZIP_DEFLATED
    ) as package:
        for path in _release_files():
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            package.write(path, relative)
            files.append({
                "path": relative,
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            })
        manifest = {
            "name": "quant-trading",
            "version": version,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "file_count": len(files),
            "files": files,
        }
        package.writestr(
            "release-manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
        package.writestr(
            "checksums.sha256",
            "\n".join(
                f"{item['sha256']}  {item['path']}"
                for item in files
            ) + "\n",
        )
    return {
        "version": version,
        "archive": str(archive),
        "sha256": _sha256(archive),
        "bytes": archive.stat().st_size,
        "file_count": len(files),
    }


def _release_files():
    for name in INCLUDE:
        path = PROJECT_ROOT / name
        if not path.exists():
            continue
        if path.is_file():
            yield path
            continue
        for child in sorted(path.rglob("*")):
            if child.is_file() and _allowed(child):
                yield child


def _allowed(path):
    if any(part in EXCLUDED_PARTS for part in path.parts):
        return False
    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return False
    if path.name.endswith(".env"):
        return False
    return True


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description="创建发布包")
    parser.add_argument("--version", required=True)
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args(argv)
    result = build_release(args.version, args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
