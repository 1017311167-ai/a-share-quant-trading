"""加载隔离配置并启动交易进程。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ops.config import load_profile
from ops.logging_setup import configure_logging, log_event
from ops.metrics import start_metrics_server


def build_parser():
    parser = argparse.ArgumentParser(description="量化交易服务入口")
    parser.add_argument("--profile", default=os.getenv("OPS_PROFILE", "paper"))
    parser.add_argument("--account", default=os.getenv("OPS_ACCOUNT"))
    parser.add_argument("--config-root", default="configs")
    parser.add_argument("--metrics-host", default="127.0.0.1")
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=int(os.getenv("OPS_METRICS_PORT", "9108")),
    )
    parser.add_argument("runner_args", nargs=argparse.REMAINDER)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    profile = load_profile(
        args.profile,
        args.account,
        config_root=Path(args.config_root),
        apply=True,
    )
    configure_logging(force=True)
    logger = configure_logging().getChild("service")
    server = None
    if args.metrics_port > 0:
        server = start_metrics_server(
            args.metrics_host, args.metrics_port
        )
        # 指标端口由 service 统一暴露，避免 runner 重复绑定。
        os.environ["OPS_METRICS_PORT"] = "0"
    log_event(
        logger,
        "service_started",
        "交易服务入口已启动",
        profile=profile.profile,
        account=profile.account,
        config_sources=profile.sources,
        metrics_port=args.metrics_port,
    )
    runner_args = list(args.runner_args)
    if runner_args and runner_args[0] == "--":
        runner_args = runner_args[1:]
    try:
        from trading.runner import main as runner_main

        return runner_main(runner_args)
    finally:
        if server is not None:
            server.shutdown()
        log_event(
            logger,
            "service_stopped",
            "交易服务入口已停止",
            profile=profile.profile,
        )


if __name__ == "__main__":
    raise SystemExit(main())
