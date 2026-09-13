"""本地不可变行情快照存储。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd

from data.models import (
    SCHEMA_VERSION,
    STANDARD_COLUMNS,
    DataQualityReport,
    DataRequest,
    SnapshotManifest,
    stable_json_hash,
)


class SnapshotStore:
    """以内容哈希和请求元数据标识数据版本。"""

    def __init__(self, root):
        self.root = Path(root)

    def write(self, request: DataRequest, df: pd.DataFrame,
              quality: DataQualityReport, *, provider: str,
              calendar_version: str, code_version: str = "unknown",
              ) -> SnapshotManifest:
        frame = df[STANDARD_COLUMNS].copy().sort_values("date").reset_index(drop=True)
        content = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
        content_hash = hashlib.sha256(content).hexdigest()
        identity = {
            "symbol": request.symbol,
            "frequency": request.frequency.value,
            "adjust": request.adjust.value,
            "requested_start": request.start.isoformat(),
            "requested_end": request.end.isoformat(),
            "content_sha256": content_hash,
            "schema_version": SCHEMA_VERSION,
            "calendar_version": calendar_version,
            "provider": provider,
        }
        snapshot_id = stable_json_hash(identity, length=20)
        snapshot_dir = self._snapshot_dir(request)
        data_path = snapshot_dir / f"{snapshot_id}.csv"
        meta_path = snapshot_dir / f"{snapshot_id}.json"
        manifest = SnapshotManifest(
            snapshot_id=snapshot_id,
            symbol=request.symbol,
            frequency=request.frequency.value,
            adjust=request.adjust.value,
            requested_start=request.start.isoformat(),
            requested_end=request.end.isoformat(),
            actual_start=quality.actual_start,
            actual_end=quality.actual_end,
            rows=len(frame),
            columns=list(frame.columns),
            content_sha256=content_hash,
            schema_version=SCHEMA_VERSION,
            calendar_source=quality.calendar_source,
            calendar_version=calendar_version,
            provider=provider,
            quality=quality.to_dict(),
            created_at=datetime.now().isoformat(timespec="seconds"),
            code_version=code_version,
        )
        if data_path.exists() and meta_path.exists():
            try:
                return SnapshotManifest.from_dict(
                    json.loads(meta_path.read_text(encoding="utf-8"))
                )
            except Exception:
                pass
        if not data_path.exists():
            _atomic_write_bytes(data_path, content)
        if not meta_path.exists():
            _atomic_write_text(
                meta_path,
                json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
            )
        return manifest

    def find(self, request: DataRequest) -> SnapshotManifest | None:
        snapshot_dir = self._snapshot_dir(request)
        if not snapshot_dir.exists():
            return None
        candidates = []
        for path in snapshot_dir.glob("*.json"):
            try:
                manifest = SnapshotManifest.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except Exception:
                continue
            if (
                manifest.requested_start == request.start.isoformat()
                and manifest.requested_end == request.end.isoformat()
                and manifest.adjust == request.adjust.value
            ):
                candidates.append(manifest)
        if not candidates:
            return None
        authoritative = [
            item for item in candidates if item.calendar_source == "akshare"
        ]
        return max(
            authoritative or candidates,
            key=lambda item: item.created_at,
        )

    def load(self, manifest: SnapshotManifest | str, *,
             symbol: str | None = None) -> pd.DataFrame:
        if isinstance(manifest, str):
            manifest = self.get(manifest, symbol=symbol)
        path = Path(manifest.path) if hasattr(manifest, "path") else None
        if path is None:
            request = DataRequest(
                symbol=manifest.symbol,
                start=manifest.requested_start,
                end=manifest.requested_end,
                frequency=manifest.frequency,
                adjust=manifest.adjust,
            )
            path = self._snapshot_dir(request) / f"{manifest.snapshot_id}.csv"
        if not path.exists():
            raise FileNotFoundError(f"数据快照不存在：{path}")
        frame = pd.read_csv(path)
        frame["date"] = pd.to_datetime(frame["date"])
        return frame

    def get(self, snapshot_id: str, *, symbol: str | None = None) -> SnapshotManifest:
        roots = [self.root / symbol] if symbol else list(self.root.glob("*"))
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob(f"{snapshot_id}.json"):
                return SnapshotManifest.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
        raise FileNotFoundError(f"未找到数据快照：{snapshot_id}")

    def list(self, *, symbol: str | None = None) -> list[SnapshotManifest]:
        root = self.root / symbol if symbol else self.root
        if not root.exists():
            return []
        manifests = []
        for path in root.rglob("*.json"):
            try:
                manifests.append(SnapshotManifest.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                ))
            except Exception:
                continue
        return sorted(manifests, key=lambda item: item.created_at, reverse=True)

    def manifest_path(self, manifest: SnapshotManifest) -> Path:
        request = DataRequest(
            symbol=manifest.symbol,
            start=manifest.requested_start,
            end=manifest.requested_end,
            frequency=manifest.frequency,
            adjust=manifest.adjust,
        )
        return self._snapshot_dir(request) / f"{manifest.snapshot_id}.json"

    def _snapshot_dir(self, request: DataRequest) -> Path:
        return (
            self.root
            / request.symbol
            / request.frequency.value
            / request.adjust.value
        )


def _atomic_write_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _atomic_write_text(path: Path, text: str):
    _atomic_write_bytes(path, text.encode("utf-8"))
