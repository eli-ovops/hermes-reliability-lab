"""v2 Telegram intake and execution orchestration.

The runtime is fail-closed: shadow is the default behavior, and live mode cannot
be constructed without an explicit TradingService. Telegram parsing always
commits to the ledger before any execution is attempted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from src_v2.executor import ExecutionOutcome
from src_v2.ingest import IngestResult, IngestStatus
from src_v2.notifications import NotificationDispatcher
from src_v2.service import TradingService
from src_v2.tg_adapter import TelegramAdapter


class RuntimeMode(str, Enum):
    SHADOW = "shadow"
    LIVE = "live"


@dataclass(frozen=True)
class RuntimeResult:
    ingest: IngestResult
    execution: ExecutionOutcome | None = None

    @property
    def strategy_id(self) -> str | None:
        return self.ingest.strategy_id


class StrategyRuntime:
    """Public seam used by Telegram handlers and offline acceptance tests."""

    def __init__(
        self,
        *,
        adapter: TelegramAdapter,
        mode: RuntimeMode = RuntimeMode.SHADOW,
        service: TradingService | None = None,
        defer_execution: bool = False,
    ):
        if mode is RuntimeMode.LIVE and service is None:
            raise ValueError("live 模式必须显式提供 TradingService")
        self.adapter = adapter
        self.mode = mode
        self.service = service
        self.defer_execution = defer_execution

    @property
    def ledger(self):
        return self.adapter.intake.ledger

    def on_new_message(
        self,
        *,
        chat_id: int,
        sender_id: int,
        sender_username: str,
        message_id: int,
        message_date: datetime,
        text: str,
    ) -> RuntimeResult:
        ingest = self.adapter.on_new_message(
            chat_id=chat_id,
            sender_id=sender_id,
            sender_username=sender_username,
            message_id=message_id,
            message_date=message_date,
            text=text,
        )
        if (
            self.defer_execution
            or self.mode is RuntimeMode.SHADOW
            or ingest.status is not IngestStatus.RECORDED
            or not ingest.strategy_id
        ):
            return RuntimeResult(ingest=ingest)
        if self.service is None:  # Defensive guard beyond constructor validation.
            raise RuntimeError("live 运行时缺少 TradingService")
        execution = self.service.execute_strategy(ingest.strategy_id)
        return RuntimeResult(ingest=ingest, execution=execution)

    def on_edited_message(self, **event) -> RuntimeResult:
        return RuntimeResult(ingest=self.adapter.on_edited_message(**event))

    def on_deleted_message(self, **event) -> RuntimeResult:
        return RuntimeResult(ingest=self.adapter.on_deleted_message(**event))


class RuntimeNotifications:
    """Deliver the runtime's durable outbox without bypassing its ledger."""

    def __init__(self, runtime: StrategyRuntime):
        self.dispatcher = NotificationDispatcher(ledger=runtime.ledger)

    def flush_once(self, sender) -> tuple[int, int]:
        return self.dispatcher.flush(
            sender,
            backoff_base=30.0,
            max_attempts=1,
        )
