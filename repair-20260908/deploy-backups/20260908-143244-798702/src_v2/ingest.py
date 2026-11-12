from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from src_v2.ledger import Ledger
from src_v2.parser import DecisionKind, ParseDecision, analyze_message


class IngestStatus(str, Enum):
    REJECTED_SOURCE = "REJECTED_SOURCE"
    IGNORED = "IGNORED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    RECORDED = "RECORDED"


@dataclass(frozen=True)
class SourceEnvelope:
    chat_id: int
    sender_id: int
    sender_username: str
    message_id: int
    message_date: datetime
    text: str
    event_kind: str = "new"


@dataclass(frozen=True)
class IngestResult:
    status: IngestStatus
    decision: ParseDecision | None = None
    strategy_id: str | None = None


class Intake:
    def __init__(
        self,
        *,
        ledger: Ledger,
        allowed_chat_ids: set[int],
        allowed_sender_ids: set[int],
        now_provider=None,
        max_message_age: timedelta = timedelta(minutes=5),
    ):
        if not allowed_chat_ids or not allowed_sender_ids:
            raise ValueError("必须配置固定群 ID 和发送者 ID")
        self.ledger = ledger
        self.allowed_chat_ids = frozenset(allowed_chat_ids)
        self.allowed_sender_ids = frozenset(allowed_sender_ids)
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.max_message_age = max_message_age

    def ingest(self, envelope: SourceEnvelope) -> IngestResult:
        if envelope.chat_id not in self.allowed_chat_ids:
            return IngestResult(IngestStatus.REJECTED_SOURCE)
        if envelope.event_kind == "deleted":
            # Deletion events have no sender ID. Only an already recorded
            # strategy proves a connection to this executor's trading state.
            strategy = self.ledger.get_strategy_by_source(
                envelope.chat_id, envelope.message_id
            )
            if strategy is None:
                decision = ParseDecision(kind=DecisionKind.IGNORE)
                self._audit(envelope, decision)
                return IngestResult(IngestStatus.IGNORED, decision=decision)
            pause_reason = "帮主群消息被删除，等待用户确认"
            self.ledger.pause_automation(pause_reason)
            result = IngestResult(
                IngestStatus.NEEDS_REVIEW,
                decision=ParseDecision(
                    kind=DecisionKind.NEEDS_REVIEW,
                    questions=("消息被删除：保留原订单，暂停新自动化并询问用户",),
                ),
            )
            self._queue_review(envelope, result.decision)
            self._audit(envelope, result.decision)
            return result
        if envelope.sender_id not in self.allowed_sender_ids:
            return IngestResult(IngestStatus.REJECTED_SOURCE)
        if envelope.event_kind == "edited":
            result = IngestResult(
                IngestStatus.IGNORED,
                decision=ParseDecision(kind=DecisionKind.IGNORE),
            )
            self._audit(envelope, result.decision)
            return result
        if envelope.event_kind != "new":
            return IngestResult(IngestStatus.IGNORED)
        pause_reason = self.ledger.automation_pause_reason()
        if pause_reason:
            decision = ParseDecision(
                kind=DecisionKind.NEEDS_REVIEW,
                questions=(f"自动化已暂停：{pause_reason}；需用户明确恢复",),
            )
            self._audit(envelope, decision)
            return IngestResult(IngestStatus.NEEDS_REVIEW, decision=decision)
        message_date = envelope.message_date
        if message_date.tzinfo is None:
            message_date = message_date.replace(tzinfo=timezone.utc)
        now = self.now_provider()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        age = now - message_date
        if age > self.max_message_age:
            decision = ParseDecision(
                kind=DecisionKind.NEEDS_REVIEW,
                questions=("Telegram 策略消息已超过5分钟有效期，禁止自动执行",),
            )
            self._queue_review(envelope, decision)
            self._audit(envelope, decision)
            return IngestResult(IngestStatus.NEEDS_REVIEW, decision=decision)
        decision = analyze_message(envelope.text)
        if decision.kind == DecisionKind.IGNORE:
            self._audit(envelope, decision)
            return IngestResult(IngestStatus.IGNORED, decision=decision)
        if decision.kind is not DecisionKind.READY or not decision.signals:
            self._queue_review(envelope, decision)
            self._audit(envelope, decision)
            return IngestResult(IngestStatus.NEEDS_REVIEW, decision=decision)
        strategy_id = f"tg-{envelope.chat_id}-{envelope.message_id}"
        self.ledger.record_strategy(
            strategy_id=strategy_id,
            symbol=decision.signals[0].symbol,
            source_chat_id=envelope.chat_id,
            source_sender_id=envelope.sender_id,
            source_message_id=envelope.message_id,
            raw_text=envelope.text,
            directions=tuple(signal.direction for signal in decision.signals),
            signals=decision.signals,
        )
        self._audit(envelope, decision)
        return IngestResult(
            IngestStatus.RECORDED,
            decision=decision,
            strategy_id=strategy_id,
        )

    def _audit(
        self, envelope: SourceEnvelope, decision: ParseDecision
    ) -> None:
        self.ledger.record_ingest_event(
            chat_id=envelope.chat_id,
            sender_id=envelope.sender_id,
            message_id=envelope.message_id,
            event_kind=envelope.event_kind,
            raw_text=envelope.text,
            decision_kind=decision.kind.value,
            reasons="；".join(decision.questions),
        )

    def _queue_review(
        self, envelope: SourceEnvelope, decision: ParseDecision
    ) -> None:
        questions = "；".join(decision.questions) or "策略需要人工确认"
        self.ledger.enqueue_notification(
            dedupe_key=(
                f"review:{envelope.chat_id}:{envelope.message_id}:"
                f"{envelope.event_kind}"
            ),
            kind="REVIEW_REQUIRED",
            message=questions,
        )
