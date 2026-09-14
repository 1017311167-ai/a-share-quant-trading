"""带冷却和去重键的告警管理。"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from pathlib import Path

from ops.logging_setup import log_event
from ops.redaction import redact


class AlertManager:
    def __init__(
            self,
            notifier=None,
            *,
            cooldown_seconds=300,
            state_file=None,
            clock=None,
    ):
        self.notifier = notifier
        self.cooldown_seconds = max(0, float(cooldown_seconds))
        self.state_file = (
            Path(state_file) if state_file else None
        )
        self.clock = clock or dt.datetime.now
        self._lock = threading.RLock()
        self._last_sent = {}
        self._load_state()

    def send(self, rule, message, *, level="warning", context=None):
        now = self.clock()
        with self._lock:
            last = self._last_sent.get(rule)
            if last is not None:
                elapsed = (now - last).total_seconds()
                if elapsed < self.cooldown_seconds:
                    return {
                        "sent": False,
                        "reason": "cooldown",
                        "remaining": self.cooldown_seconds - elapsed,
                    }
            self._last_sent[rule] = now
            self._save_state()
        logger = logging.getLogger("ops.alerts")
        log_event(
            logger,
            "alert",
            message,
            level=logging.WARNING if level == "warning" else logging.ERROR,
            rule=rule,
            alert_level=level,
            **(context or {}),
        )
        if self.notifier is None:
            result = {}
        else:
            payload = {
                "告警规则": rule,
                "级别": level,
                "内容": message,
                **redact(context or {}, account=True),
            }
            result = self.notifier.send_risk_alert(payload)
        return {"sent": True, "channels": result}

    def _load_state(self):
        if self.state_file is None or not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            self._last_sent = {
                key: dt.datetime.fromisoformat(value)
                for key, value in data.items()
            }
        except (OSError, ValueError, TypeError):
            self._last_sent = {}

    def _save_state(self):
        if self.state_file is None:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            key: value.isoformat()
            for key, value in self._last_sent.items()
        }
        temp = self.state_file.with_suffix(".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(self.state_file)
