"""跨平台进程守护、指数退避和自动重启。"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from ops.logging_setup import configure_logging, log_event


class ProcessSupervisor:
    def __init__(
            self,
            command,
            *,
            cwd=None,
            env=None,
            stop_file=None,
            state_file=None,
            max_restarts=20,
            restart_window_seconds=3600,
            initial_backoff=1.0,
            max_backoff=60.0,
            poll_interval=1.0,
            process_factory=None,
            clock=None,
            sleeper=None,
    ):
        self.command = list(command)
        if not self.command:
            raise ValueError("守护进程命令不能为空")
        self.cwd = Path(cwd or Path.cwd())
        self.env = dict(env or os.environ)
        self.stop_file = Path(stop_file) if stop_file else None
        self.state_file = Path(state_file) if state_file else None
        self.max_restarts = max(0, int(max_restarts))
        self.restart_window_seconds = max(
            1.0, float(restart_window_seconds)
        )
        self.initial_backoff = max(0.0, float(initial_backoff))
        self.max_backoff = max(self.initial_backoff, float(max_backoff))
        self.poll_interval = max(0.05, float(poll_interval))
        self.process_factory = process_factory or subprocess.Popen
        self.clock = clock or dt.datetime.now
        self.sleeper = sleeper or time.sleep
        self.restarts = []
        self.current_process = None
        self.stopping = False

    def run(self):
        log_event(
            _logger(),
            "supervisor_started",
            "进程守护已启动",
            command=self.command,
            cwd=str(self.cwd),
        )
        exit_code = None
        while not self.stopping:
            if self._stop_requested():
                self.stopping = True
                break
            self.current_process = self.process_factory(
                self.command,
                cwd=str(self.cwd),
                env=self.env,
                **_platform_process_kwargs(),
            )
            self._write_state("running")
            exit_code = self._wait()
            self._write_state("stopped")
            if self.stopping or self._stop_requested():
                self.stopping = True
                break
            if exit_code == 0:
                log_event(
                    _logger(),
                    "supervisor_child_exit",
                    "子进程正常退出，守护结束",
                    exit_code=exit_code,
                )
                break
            if not self._can_restart():
                log_event(
                    _logger(),
                    "supervisor_restart_limit",
                    "达到重启上限，守护退出",
                    exit_code=exit_code,
                    max_restarts=self.max_restarts,
                )
                break
            backoff = self._backoff()
            self.restarts.append(self.clock())
            log_event(
                _logger(),
                "supervisor_restart",
                "子进程异常退出，准备自动重启",
                exit_code=exit_code,
                backoff_seconds=backoff,
                restart_count=len(self.restarts),
            )
            self.sleeper(backoff)
        self._terminate_current()
        self._write_state("supervisor_stopped")
        return exit_code

    def stop(self):
        self.stopping = True
        self._terminate_current()

    def _wait(self):
        while not self.stopping:
            result = self.current_process.poll()
            if result is not None:
                return int(result)
            if self._stop_requested():
                self.stopping = True
                self._terminate_current()
                try:
                    return int(self.current_process.wait(timeout=10))
                except TypeError:
                    return 0
            self.sleeper(self.poll_interval)
        return 0

    def _terminate_current(self):
        process = self.current_process
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            try:
                process.wait(timeout=10)
            except TypeError:
                pass
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _can_restart(self):
        cutoff = self.clock().timestamp() - self.restart_window_seconds
        self.restarts = [
            item for item in self.restarts if item.timestamp() >= cutoff
        ]
        return len(self.restarts) < self.max_restarts

    def _backoff(self):
        exponent = min(6, len(self.restarts))
        return min(
            self.max_backoff,
            self.initial_backoff * (2 ** exponent),
        )

    def _stop_requested(self):
        return self.stop_file is not None and self.stop_file.exists()

    def _write_state(self, status):
        if self.state_file is None:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "status": status,
            "updated_at": self.clock().isoformat(),
            "pid": (
                self.current_process.pid
                if self.current_process is not None else None
            ),
            "restart_count": len(self.restarts),
            "command": self.command,
        }
        temp = self.state_file.with_suffix(".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(self.state_file)


def _platform_process_kwargs():
    if sys.platform != "win32":
        return {}
    creation_flags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    return {"creationflags": creation_flags}


def _logger():
    import logging

    return logging.getLogger("ops.supervisor")


def main(argv=None):
    parser = argparse.ArgumentParser(description="交易进程守护")
    parser.add_argument("--stop-file", default="logs/paper.stop")
    parser.add_argument("--state-file", default="logs/supervisor.json")
    parser.add_argument("--max-restarts", type=int, default=20)
    parser.add_argument("--restart-window", type=float, default=3600)
    parser.add_argument("--initial-backoff", type=float, default=1.0)
    parser.add_argument("--max-backoff", type=float, default=60.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        command = [sys.executable, "-m", "trading.runner"]
    configure_logging()
    supervisor = ProcessSupervisor(
        command,
        stop_file=args.stop_file,
        state_file=args.state_file,
        max_restarts=args.max_restarts,
        restart_window_seconds=args.restart_window,
        initial_backoff=args.initial_backoff,
        max_backoff=args.max_backoff,
    )

    def handle_stop(signum, frame):
        supervisor.stop()

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), handle_stop)
    return supervisor.run() or 0


if __name__ == "__main__":
    raise SystemExit(main())
