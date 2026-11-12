"""微信通知发送器 — 通过 Hermes 微信通道发送。

本模块只做接线，不直接发送。真实发送须经用户明确批准。
send() 抛异常时由 NotificationDispatcher 保留 PENDING 并退避重试。
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable


class WeChatSender:
    def __init__(
        self,
        *,
        hermes_bin: str = "/Users/USER/.hermes/hermes-agent/venv/bin/hermes",
        target: str = "weixin",
        timeout: int = 20,
    ):
        self.hermes_bin = hermes_bin
        self.target = target
        self.timeout = timeout

    def send(self, message: str) -> None:
        result = subprocess.run(
            [self.hermes_bin, "send", "--to", self.target, message],
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"微信发送失败(exit={result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

    def as_callable(self) -> Callable[[str], None]:
        return self.send
