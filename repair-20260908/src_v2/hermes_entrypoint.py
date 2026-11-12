"""Hermes 唯一交易执行入口。

Hermes（及其 terminal / 代码执行）只能通过本模块执行交易写操作，
不得直接构造 OKXGateway、调用 OKX SDK 写接口或 OKX CLI 下单。

本模块是 TradingService 的薄封装：
- 只暴露 execute / recover / flush-notifications / resume / 受控重处理安全动词
- 不暴露任何原始 SDK 对象
- 凭证从 V2 专用环境变量读取，不读旧 .env
"""
from __future__ import annotations

import os
from pathlib import Path

from src_v2.notifications import NotificationDispatcher
from src_v2.parser import DecisionKind, analyze_message, analyze_approved_range
from src_v2.process_lock import ExclusiveProcessLock
from src_v2.sdk_factory import build_gateway
from src_v2.service import TradingService


def _credentials() -> tuple[str, str, str]:
    api_key = os.environ.get("V2_OKX_API_KEY", "")
    api_secret = os.environ.get("V2_OKX_API_SECRET", "")
    passphrase = os.environ.get("V2_OKX_PASSPHRASE", "")
    if not (api_key and api_secret and passphrase):
        raise RuntimeError(
            "缺少 V2_OKX_API_KEY / V2_OKX_API_SECRET / V2_OKX_PASSPHRASE；"
            "Hermes 不得读取旧 .env 中的生产凭证"
        )
    return api_key, api_secret, passphrase


def _ledger_path() -> Path:
    return Path(os.environ.get("V2_LEDGER_PATH", "v2_ledger.sqlite3"))


def _build_service(*, demo: bool) -> TradingService:
    from src_v2.ledger import Ledger

    api_key, api_secret, passphrase = _credentials()
    gateway = build_gateway(
        api_key=api_key,
        api_secret=api_secret,
        passphrase=passphrase,
        demo=demo,
        proxy=os.environ.get("V2_OKX_PROXY"),
    )
    ledger = Ledger(_ledger_path())
    return TradingService(
        ledger=ledger,
        gateway=gateway,
        lock_path=Path(os.environ.get("V2_LOCK_PATH", "v2_executor.lock")),
        require_source_attestation=True,
    )


def execute_strategy(strategy_id: str, *, demo: bool = True):
    """执行一个已入账的策略。demo=True 只走模拟盘。"""
    service = _build_service(demo=demo)
    return service.execute_strategy(strategy_id)


def recover_attempt(attempt_id: str, *, demo: bool = True):
    """对账并恢复一个不确定的订单尝试。"""
    service = _build_service(demo=demo)
    return service.recover_attempt(attempt_id)


def reprocess_authenticated_message(
    message_id: int,
    *,
    confirmation: str,
    reason: str,
    demo: bool = True,
    approved_range: tuple[str, float, float] | None = None,
):
    """Re-evaluate one exact authenticated Telegram event after user approval."""
    if confirmation != "REPROCESS_AUTHENTICATED_RANGE":
        raise RuntimeError("重新处理历史消息缺少明确确认")
    if not reason.strip():
        raise RuntimeError("重新处理历史消息必须记录授权原因")
    try:
        chat_id = int((os.environ.get("V2_TG_CHAT_ID") or "").strip())
        sender_id = int((os.environ.get("V2_TG_SENDER_ID") or "").strip())
    except ValueError as exc:
        raise RuntimeError("已认证 Telegram 数字 ID 配置无效") from exc
    if not chat_id or not sender_id:
        raise RuntimeError("缺少已认证 Telegram 数字 ID")

    service = _build_service(demo=demo)
    ledger = service.ledger
    event = ledger.get_ingest_event(
        chat_id=chat_id,
        sender_id=sender_id,
        message_id=message_id,
        event_kind="new",
    )
    if event is None:
        raise RuntimeError("找不到与白名单数字 ID 匹配的原始消息")
    if approved_range is None:
        decision = analyze_message(event.raw_text)
    else:
        symbol, low, high = approved_range
        decision = analyze_approved_range(event.raw_text, symbol=symbol, low=low, high=high)
        reason = f"{reason}; 显式区间 {symbol} {low}-{high}; 下沿多/上沿空，止盈止损各2%"
    if decision.kind is not DecisionKind.READY or not decision.signals:
        raise RuntimeError("当前解析器仍未将该消息判定为完整可执行策略")

    strategy_id = f"tg-{chat_id}-{message_id}:user-approved-range"
    strategy = ledger.get_strategy(strategy_id)
    if strategy is None:
        ledger.authorize_ingest_reclassification(
            chat_id=chat_id,
            sender_id=sender_id,
            message_id=message_id,
            raw_text=event.raw_text,
            reason=reason,
        )
        ledger.record_strategy(
            strategy_id=strategy_id,
            symbol=decision.signals[0].symbol,
            source_chat_id=chat_id,
            source_sender_id=sender_id,
            source_message_id=message_id,
            raw_text=event.raw_text,
            directions=tuple(signal.direction for signal in decision.signals),
            signals=decision.signals,
        )
    else:
        exact_source = (
            strategy.source_chat_id == chat_id
            and strategy.source_sender_id == sender_id
            and strategy.source_message_id == message_id
            and strategy.symbol == decision.signals[0].symbol
        )
        if not exact_source:
            raise RuntimeError("历史策略 ID 与已认证消息不匹配")
    return service.execute_strategy(strategy_id)


def flush_notifications(sender, *, demo: bool = True):
    """投递通知 outbox。sender 是外部发送 callable。"""
    from src_v2.ledger import Ledger

    service = _build_service(demo=demo)
    dispatcher = NotificationDispatcher(ledger=service.ledger)
    return dispatcher.flush(sender)


def resume_automation(*, confirmation: str, reason: str) -> None:
    """Resume intake only after an exact, auditable user confirmation."""
    if confirmation != "RESUME_V2_AUTOMATION":
        raise RuntimeError("恢复自动化缺少明确确认，拒绝操作")
    if not reason.strip():
        raise RuntimeError("恢复自动化必须记录用户确认原因")
    from src_v2.ledger import Ledger

    Ledger(_ledger_path()).resume_automation(reason)
