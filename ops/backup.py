"""SQLite 一致性备份、校验、恢复和保留策略。"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path


def create_backup(
        database,
        backup_dir,
        *,
        label="manual",
        retain=30,
        clock=None,
) -> dict:
    database = Path(database)
    if not database.exists():
        raise FileNotFoundError(f"数据库不存在：{database}")
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    clock = clock or dt.datetime.now
    timestamp = clock().strftime("%Y%m%dT%H%M%S")
    stem = f"{database.stem}_{label}_{timestamp}"
    target = backup_dir / f"{stem}.db"
    source = sqlite3.connect(database)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    manifest = {
        "database": str(database),
        "backup": str(target),
        "created_at": clock().isoformat(),
        "sha256": sha256_file(target),
        "bytes": target.stat().st_size,
        "label": label,
    }
    manifest_path = backup_dir / f"{stem}.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    prune_backups(backup_dir, retain=retain)
    return manifest


def verify_backup(manifest_or_path) -> dict:
    manifest_path = _manifest_path(manifest_or_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    backup = Path(manifest["backup"])
    if not backup.exists():
        raise FileNotFoundError(f"备份文件不存在：{backup}")
    actual = sha256_file(backup)
    if actual != manifest["sha256"]:
        raise ValueError(
            f"备份校验失败：期望 {manifest['sha256']}，实际 {actual}"
        )
    conn = sqlite3.connect(f"file:{backup.resolve()}?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    if not result or result[0] != "ok":
        raise ValueError(f"SQLite 完整性检查失败：{result!r}")
    return {**manifest, "verified": True, "integrity_check": "ok"}


def restore_backup(
        manifest_or_path,
        target,
        *,
        confirmed=False,
        safety_backup=True,
) -> dict:
    if not confirmed:
        raise ValueError("恢复备份必须显式设置 confirmed=True")
    verified = verify_backup(manifest_or_path)
    source = Path(verified["backup"])
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    safety = None
    if target.exists() and safety_backup:
        safety = create_backup(
            target,
            target.parent / "backups",
            label="before_restore",
        )
    for sidecar in (
        target.with_name(target.name + "-wal"),
        target.with_name(target.name + "-shm"),
    ):
        if sidecar.exists():
            sidecar.unlink()
    temp = target.with_suffix(target.suffix + ".restore.tmp")
    shutil.copy2(source, temp)
    temp.replace(target)
    return {
        "restored": True,
        "target": str(target),
        "source": str(source),
        "safety_backup": safety,
    }


def list_backups(backup_dir):
    backup_dir = Path(backup_dir)
    if not backup_dir.exists():
        return []
    values = []
    for path in sorted(backup_dir.glob("*.json"), reverse=True):
        try:
            values.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return values


def prune_backups(backup_dir, *, retain=30):
    retain = max(1, int(retain))
    manifests = list_backups(backup_dir)
    removed = []
    for item in manifests[retain:]:
        backup = Path(item.get("backup", ""))
        manifest = backup.with_suffix(".json") if backup else None
        for path in (backup, manifest):
            if path and path.exists():
                path.unlink()
                removed.append(str(path))
    return removed


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_path(value):
    path = Path(value)
    if path.is_dir():
        candidates = sorted(path.glob("*.json"), reverse=True)
        if not candidates:
            raise FileNotFoundError(f"目录中没有备份清单：{path}")
        return candidates[0]
    if path.suffix == ".db":
        candidate = path.with_suffix(".json")
        if not candidate.exists():
            raise FileNotFoundError(f"找不到备份清单：{candidate}")
        return candidate
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description="交易数据库备份与恢复")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--database", required=True)
    create.add_argument("--backup-dir", required=True)
    create.add_argument("--label", default="manual")
    create.add_argument("--retain", type=int, default=30)
    verify = sub.add_parser("verify")
    verify.add_argument("backup")
    restore = sub.add_parser("restore")
    restore.add_argument("backup")
    restore.add_argument("--target", required=True)
    restore.add_argument("--confirm", action="store_true")
    listing = sub.add_parser("list")
    listing.add_argument("--backup-dir", required=True)
    args = parser.parse_args(argv)
    if args.command == "create":
        result = create_backup(
            args.database,
            args.backup_dir,
            label=args.label,
            retain=args.retain,
        )
    elif args.command == "verify":
        result = verify_backup(args.backup)
    elif args.command == "restore":
        result = restore_backup(
            args.backup, args.target, confirmed=args.confirm
        )
    else:
        result = list_backups(args.backup_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
