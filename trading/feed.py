"""持续运行使用的信号输入。"""

from __future__ import annotations

import json
from pathlib import Path

from trading.models import TradingSignal


class ListSignalFeed:
    def __init__(self, signals):
        self._signals = list(signals)
        self._index = 0

    def poll(self):
        if self._index >= len(self._signals):
            return []
        values = self._signals[self._index:]
        self._index = len(self._signals)
        return values


class JsonlSignalFeed:
    """增量读取 JSON Lines 信号文件，适合研究进程与交易进程解耦。"""

    def __init__(self, path):
        self.path = Path(path)
        self._offset = 0
        self._buffer = ""

    def poll(self):
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as handle:
            handle.seek(self._offset)
            chunk = handle.read()
            self._offset = handle.tell()
        if not chunk:
            return []
        self._buffer += chunk
        lines = self._buffer.splitlines(keepends=True)
        self._buffer = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._buffer = lines.pop()
        signals = []
        for line in lines:
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            signals.append(TradingSignal.from_dict(json.loads(text)))
        return signals
