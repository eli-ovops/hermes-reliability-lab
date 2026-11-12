"""Fail-closed process entrypoint for the v2 Telegram trading runtime."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import hmac
import os
import logging
import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import urlparse

from src_v2.hermes_entrypoint import _build_service
from src_v2.ingest import Intake
from src_v2.ledger import Ledger
from src_v2.runtime import RuntimeMode, RuntimeNotifications, StrategyRuntime
from src_v2.sdk_factory import server_time_skew_seconds, verify_account_config
from src_v2.tg_adapter import TelegramAdapter
from src_v2.tg_runner import run_listener
from src_v2.telegram_config import load_telegram_api_credentials
from src_v2.wechat_sender import WeChatSender


LIVE_ACK = "ENABLE_V2_LIVE_TRADING"
LEGACY_LABELS = ("com.bangzhu.bottg", "com.bangzhu.botokx")


def _required_int(env: Mapping[str, str], key: str) -> int:
    """读取必需整数环境变量；缺失、为空或格式错误一律拒绝。"""
    raw = (env.get(key) or "").strip()
    if not raw:
        raise RuntimeError(f"缺少必需环境变量 {key}")
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"环境变量 {key} 格式无效: {raw!r}") from exc


def _required_account_fingerprint(env: Mapping[str, str]) -> str:
    raw = (env.get("V2_OKX_ACCOUNT_FINGERPRINT") or "").strip()
    if not raw:
        raise RuntimeError("缺少必需环境变量 V2_OKX_ACCOUNT_FINGERPRINT")
    if re.fullmatch(r"[0-9a-fA-F]{64}", raw) is None:
        raise RuntimeError("V2_OKX_ACCOUNT_FINGERPRINT 格式无效")
    return raw.lower()


def _assert_live_account_binding(env: Mapping[str, str]) -> None:
    expected = _required_account_fingerprint(env)
    ledger = Ledger(Path(env.get("V2_LEDGER_PATH", "v2_ledger.sqlite3")))
    actual = ledger.account_fingerprint()
    if actual is None:
        raise RuntimeError("实盘账本尚未绑定 OKX 账号，拒绝启动")
    if not hmac.compare_digest(actual, expected):
        raise RuntimeError("实盘账本绑定的 OKX 账号不匹配，拒绝启动")


def build_verified_live_service(
    *,
    expected_account_fingerprint: str,
    service_factory: Callable[[], object] = lambda: _build_service(demo=False),
    time_check: Callable[[object], object] = server_time_skew_seconds,
    account_check: Callable[[object], object] = verify_account_config,
):
    service = service_factory()
    time_check(service.gateway.market)
    account_summary = account_check(service.gateway.account)
    actual_fingerprint = (
        account_summary.get("account_fingerprint")
        if isinstance(account_summary, Mapping)
        else None
    )
    if not isinstance(actual_fingerprint, str) or not hmac.compare_digest(
        actual_fingerprint, expected_account_fingerprint
    ):
        raise RuntimeError("OKX 账号身份不匹配，拒绝启动实盘执行器")
    return service


def assert_legacy_writers_stopped() -> None:
    """Reject live startup while a legacy LaunchAgent or process is present."""
    domain = f"gui/{os.getuid()}"
    active: list[str] = []
    for label in LEGACY_LABELS:
        result = subprocess.run(
            ["launchctl", "print", f"{domain}/{label}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            active.append(label)
    for pattern in ("[b]ot_tg.py", "[b]ot_okx.py"):
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            active.append(pattern.replace("[", "").replace("]", ""))
    if active:
        raise RuntimeError(
            "旧交易写入者仍在运行或已加载，拒绝启动 v2 live: "
            + ", ".join(dict.fromkeys(active))
        )


def build_runtime(
    *,
    mode: RuntimeMode,
    env: Mapping[str, str],
    live_service_factory: Callable[[], object],
    legacy_writer_guard: Callable[[], None] = assert_legacy_writers_stopped,
) -> StrategyRuntime:
    if mode is RuntimeMode.LIVE:
        if env.get("V2_LIVE_TRADING_ACK") != LIVE_ACK:
            raise RuntimeError("live 模式缺少明确确认，拒绝启动")
        if env.get("V2_WECHAT_ENABLED") != "1":
            raise RuntimeError("live 模式必须启用微信通知，拒绝静默交易")
        legacy_writer_guard()
    ledger = Ledger(Path(env.get("V2_LEDGER_PATH", "v2_ledger.sqlite3")))
    chat_id = _required_int(env, "V2_TG_CHAT_ID")
    sender_id = _required_int(env, "V2_TG_SENDER_ID")
    adapter = TelegramAdapter(
        intake=Intake(
            ledger=ledger,
            allowed_chat_ids={chat_id},
            allowed_sender_ids={sender_id},
            dispatch_enabled=mode is RuntimeMode.LIVE,
        )
    )
    if mode is RuntimeMode.SHADOW:
        return StrategyRuntime(adapter=adapter, mode=mode)
    service = live_service_factory()
    return StrategyRuntime(adapter=adapter, mode=mode, service=service, defer_execution=True)


def _proxy_from_env(env: Mapping[str, str]):
    value = env.get("V2_TG_PROXY", "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"socks5", "socks4", "http"}:
        raise RuntimeError("V2_TG_PROXY 只允许 socks5、socks4 或 http")
    if not parsed.hostname or not parsed.port:
        raise RuntimeError("V2_TG_PROXY 格式无效")
    return (parsed.scheme, parsed.hostname, parsed.port)


async def _run(mode: RuntimeMode, env: Mapping[str, str]) -> int:
    api_id, api_hash = load_telegram_api_credentials(env)
    if mode is RuntimeMode.LIVE:
        _assert_live_account_binding(env)
    runtime = build_runtime(
        mode=mode,
        env=env,
        live_service_factory=lambda: build_verified_live_service(
            expected_account_fingerprint=_required_account_fingerprint(env)
        ),
    )
    await _startup_reconciliation(runtime)
    listener = asyncio.create_task(
        run_listener(
            runtime,
            api_id=api_id,
            api_hash=api_hash,
            session_name=env.get("V2_TG_SESSION", "bangzhu_v2_shadow.session"),
            proxy=_proxy_from_env(env),
            allowed_chats={_required_int(env, "V2_TG_CHAT_ID")},
        )
    )
    if env.get("V2_WECHAT_ENABLED") != "1":
        await listener
        return 0
    sender = WeChatSender(
        hermes_bin=env.get(
            "HERMES_BIN", "/Users/USER/.hermes/hermes-agent/venv/bin/hermes"
        ),
        target=env.get("V2_WECHAT_TARGET", "weixin"),
    ).send
    pump = asyncio.create_task(_notification_loop(runtime, sender))
    watcher = (
        asyncio.create_task(_reconciliation_loop(runtime))
        if mode is RuntimeMode.LIVE
        else None
    )
    tasks = {listener, pump}
    if watcher is not None:
        tasks.add(watcher)
    done, pending = await asyncio.wait(
        tasks, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    for task in pending:
        with suppress(asyncio.CancelledError):
            await task
    if pump in done:
        await pump
        raise RuntimeError("微信通知循环意外停止")
    if watcher is not None and watcher in done:
        await watcher
        raise RuntimeError("订单持续对账循环意外停止")
    await listener
    return 0


async def _notification_loop(
    runtime: StrategyRuntime, sender, *, interval: float = 30.0
) -> None:
    notifications = RuntimeNotifications(runtime)
    while True:
        await asyncio.to_thread(notifications.flush_once, sender)
        await asyncio.sleep(interval)


async def _reconciliation_loop(
    runtime: StrategyRuntime,
    *,
    interval: float = 10.0,
    sleep_fn=None,
) -> None:
    """Continuously reconcile exchange state and protect later fills."""
    if runtime.mode is not RuntimeMode.LIVE or runtime.service is None:
        raise RuntimeError("持续对账只允许在已验证的 live runtime 中运行")
    sleep = sleep_fn or asyncio.sleep
    while True:
        await sleep(interval)
        try:
            await asyncio.to_thread(runtime.service.reconcile_open_attempts)
            await asyncio.to_thread(runtime.service.dispatch_pending)
        except Exception as exc:
            # Keep the receiver and notification pump alive during a failed
            # reconciliation cycle. Dispatch waits until a clean cycle.
            logging.getLogger(__name__).error("执行周期失败: %s", type(exc).__name__)


async def _startup_reconciliation(runtime: StrategyRuntime) -> None:
    """Close recovery gaps before Telegram can deliver a new strategy."""
    if runtime.mode is RuntimeMode.SHADOW:
        return
    if runtime.service is None:
        raise RuntimeError("live 启动对账缺少 TradingService")
    await asyncio.to_thread(runtime.service.reconcile_open_attempts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="乔帮主 v2 常驻监听器")
    parser.add_argument(
        "--mode",
        choices=tuple(item.value for item in RuntimeMode),
        default=RuntimeMode.SHADOW.value,
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(RuntimeMode(args.mode), os.environ))


if __name__ == "__main__":
    raise SystemExit(main())
