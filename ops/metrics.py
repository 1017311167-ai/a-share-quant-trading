"""轻量 Prometheus 指标注册表和 HTTP 暴露端点。"""

from __future__ import annotations

import math
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MetricsRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        self._counters = defaultdict(float)
        self._gauges = {}
        self._histograms = defaultdict(list)
        self._help = {}

    def counter(self, name, value=1.0, **labels):
        key = _series(name, labels)
        with self._lock:
            self._counters[key] += float(value)

    def gauge(self, name, value, **labels):
        key = _series(name, labels)
        with self._lock:
            self._gauges[key] = float(value)

    def histogram(self, name, value, **labels):
        key = _series(name, labels)
        with self._lock:
            self._histograms[key].append(float(value))

    def describe(self, name, help_text):
        with self._lock:
            self._help[name] = str(help_text)

    def snapshot(self):
        with self._lock:
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "histograms": {
                    key: list(values)
                    for key, values in self._histograms.items()
                },
            }

    def prometheus(self):
        lines = []
        with self._lock:
            for key in sorted(self._counters):
                name, labels = _split_series(key)
                lines.append(
                    f"# HELP {name} {self._help.get(name, name)}"
                )
                lines.append("# TYPE %s counter" % name)
                lines.append(f"{name}{labels} {_number(self._counters[key])}")
            for key in sorted(self._gauges):
                name, labels = _split_series(key)
                lines.append(
                    f"# HELP {name} {self._help.get(name, name)}"
                )
                lines.append("# TYPE %s gauge" % name)
                lines.append(f"{name}{labels} {_number(self._gauges[key])}")
            for key in sorted(self._histograms):
                name, labels = _split_series(key)
                values = self._histograms[key]
                count = len(values)
                total = sum(values)
                lines.append(
                    f"# HELP {name} {self._help.get(name, name)}"
                )
                lines.append("# TYPE %s summary" % name)
                lines.append(
                    f"{name}_count{labels} {count}"
                )
                lines.append(
                    f"{name}_sum{labels} {_number(total)}"
                )
        return "\n".join(lines) + "\n"


METRICS = MetricsRegistry()
METRICS.describe(
    "trading_runtime_cycles_total", "交易进程循环次数"
)
METRICS.describe(
    "trading_signals_total", "按处理结果统计的策略信号"
)
METRICS.describe(
    "trading_orders_total", "按状态统计的订单"
)
METRICS.describe(
    "trading_fills_total", "按股票和方向统计的成交"
)
METRICS.describe(
    "trading_risk_state", "当前风险状态数值"
)
METRICS.describe(
    "trading_reconciliation_differences", "最近对账差异数量"
)
METRICS.describe(
    "trading_runtime_online", "运行实例是否在线"
)
METRICS.describe(
    "trading_command_total", "运行命令处理结果"
)


def start_metrics_server(host="127.0.0.1", port=9108, registry=None):
    registry = registry or METRICS
    handler = _handler(registry)
    server = ThreadingHTTPServer((host, int(port)), handler)
    thread = threading.Thread(
        target=server.serve_forever,
        name="metrics-server",
        daemon=True,
    )
    thread.start()
    return server


def _handler(registry):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/metrics":
                body = registry.prometheus().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path in {"/health", "/healthz"}:
                body = b'{"status":"ok"}\n'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)

        def log_message(self, format, *args):
            return

    return Handler


def _series(name, labels):
    if not labels:
        return name
    text = ",".join(
        f'{key}="{_escape(value)}"'
        for key, value in sorted(labels.items())
    )
    return f"{name}{{{text}}}"


def _split_series(key):
    if "{" not in key:
        return key, ""
    return key.split("{", 1)[0], "{" + key.split("{", 1)[1]


def _escape(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _number(value):
    value = float(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return str(value)
