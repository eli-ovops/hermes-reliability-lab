"""微信通知发送器 — 通过 Hermes 微信通道发送。

本模块只做接线，不直接发送。真实发送须经用户明确批准。
send() 抛异常时由 NotificationDispatcher 保留 PENDING 并退避重试。
"""
from __future__ import annotations

import subprocess
import json
import re
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
            [self.hermes_bin, "send", "--json", "--to", self.target, message],
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=False,
        )
        try:
            response = json.loads(result.stdout)
        except (ValueError, TypeError):
            if result.returncode:
                raise RuntimeError(f"微信发送失败(exit={result.returncode}; {_safe_backend_error(result.stderr)})") from None
            raise RuntimeError("微信发送未返回有效的 JSON 回执") from None
        if result.returncode != 0 or (isinstance(response, dict) and response.get('error')):
            error = response.get('error', '') if isinstance(response, dict) else ''
            raise RuntimeError(f"微信发送失败(exit={result.returncode}; {_safe_backend_error(error)})")
        if not isinstance(response, dict) or response.get("success") is not True or response.get("skipped") or response.get("error"):
            raise RuntimeError("微信发送未确认成功，通知保留待发送")

    def as_callable(self) -> Callable[[str], None]:
        return self.send


def _safe_backend_error(error: object) -> str:
    """Report known failure categories/codes, never echo arbitrary backend text."""
    value = str(error)
    lower = value.lower()
    if 'rate limit' in lower or re.search(r'errcode\s*[=:]\s*-2\b', lower):
        category = 'WECHAT_RATE_LIMITED 微信通道限流，等待恢复'
    elif 'session expired' in lower or re.search(r'errcode\s*[=:]\s*-14\b', lower):
        category = 'WECHAT_SESSION_EXPIRED 微信会话失效'
    elif 'token missing' in lower or 'account id missing' in lower or 'not configured' in lower:
        category = 'WECHAT_CONFIGURATION_MISSING 微信配置缺失'
    elif 'timeout' in lower or 'timed out' in lower:
        category = 'WECHAT_TIMEOUT 微信通道超时'
    elif 'ssl' in lower or 'certificate' in lower:
        category = 'WECHAT_TLS_ERROR 微信通道连接校验失败'
    elif 'connect' in lower:
        category = 'WECHAT_CONNECTION_ERROR 微信通道连接失败'
    else:
        category = 'WECHAT_BACKEND_ERROR 微信后端返回未分类错误'
    codes = re.findall(r'\b(?:errcode|ret)\s*[=:]\s*-?\d{1,8}\b', value)
    return '; '.join([category] + codes[:2])
