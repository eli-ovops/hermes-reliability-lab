from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
from threading import RLock

from src_v2.executor import ExecutionOutcome, ExecutionStatus, Executor
from src_v2.ledger import Ledger, LedgerConflict
from src_v2.process_lock import ExclusiveProcessLock
from src_v2.reconciler import ReconcileResult, ReconcileStatus, Reconciler


class TradingService:
    """Single-process orchestration over durable ledger state.

    It deliberately has no credential loading or live-mode switch. Those belong to
    a separately authorized cutover entrypoint.
    """

    def __init__(
        self,
        *,
        ledger: Ledger,
        gateway,
        lock_path: str | Path,
        now_provider=None,
        require_source_attestation: bool = False,
    ):
        self.ledger = ledger
        self.gateway = gateway
        self.lock_path = Path(lock_path)
        self.now_provider = now_provider
        self.require_source_attestation = require_source_attestation
        # Serialize listener and reconciliation threads before the cross-process
        # guard. A busy sibling thread is not a second trading process.
        self._operation_lock = RLock()
        self._reconciliation_failures: dict[str, int] = {}
        self._reconciliation_episodes: dict[str, int] = {}
        # A failed exchange connection affects every open order at once.  Track
        # that outage as one notification episode instead of one message/order.
        self._transient_attempt_groups: dict[str, str] = {}
        self._transient_failure_groups: dict[str, set[str]] = {}
        self._transient_group_episodes: dict[str, int] = {}

    def _reconciler(self) -> Reconciler:
        return Reconciler(
            ledger=self.ledger,
            gateway=self.gateway,
            now_provider=self.now_provider,
        )

    def execute_strategy(self, strategy_id: str) -> ExecutionOutcome:
        with self._operation_lock, ExclusiveProcessLock(self.lock_path):
            return self._execute_unlocked(strategy_id)

    def _job_allows_automatic_retry(self, strategy_id: str) -> bool:
        job = self.ledger.get_execution_job(strategy_id)
        if job is None:
            return True  # preserve the existing recovery policy for legacy orders
        return job['state'] in {'PENDING','RUNNING','RETRY','RECONCILING'}

    def dispatch_pending(self, *, now=None):
        from src_v2.dispatch import dispatch_pending
        with self._operation_lock, ExclusiveProcessLock(self.lock_path):
            return dispatch_pending(self, now=now)

    def reconcile_open_attempts(
        self,
    ) -> tuple[tuple[str, ReconcileResult], ...]:
        """Reconcile every non-terminal attempt without creating a new entry.

        This is the safe seam for startup recovery and the runtime watcher.  It
        may attach or expand protection for confirmed fills, but it deliberately
        does not retry a missing entry; retries remain an explicit recovery act.
        """
        with self._operation_lock, ExclusiveProcessLock(self.lock_path):
            reconciler = self._reconciler()
            results: list[tuple[str, ReconcileResult]] = []
            for attempt in self.ledger.list_reconcilable_attempts():
                try:
                    result = reconciler.reconcile(attempt.attempt_id)
                except LedgerConflict as exc:
                    result = ReconcileResult(
                        ReconcileStatus.NEEDS_REVIEW,
                        f"对账期间账本状态发生变化: {exc}",
                    )
                except Exception as exc:
                    reason = f"保护核对异常 ({type(exc).__name__})；继续检查其余订单"
                    self.ledger.pause_automation(reason)
                    result = ReconcileResult(ReconcileStatus.NEEDS_REVIEW, reason)
                if (
                    result.status == ReconcileStatus.MISSING_CONFIRMED
                    and attempt.state in {"SUBMITTING", "SUBMITTED", "UNKNOWN"}
                    and self.ledger.automation_pause_reason() is None
                    and self._job_allows_automatic_retry(attempt.strategy_id)
                ):
                    strategy = self.ledger.get_strategy(attempt.strategy_id)
                    if strategy is not None and strategy.state == "ACTIVE":
                        try:
                            retry = self.ledger.prepare_confirmed_missing_retry(
                                attempt.attempt_id
                            )
                        except LedgerConflict:
                            pass
                        else:
                            self._execute_unlocked(retry.strategy_id)
                current = self.ledger.get_attempt(attempt.attempt_id)
                state = current.state if current is not None else "MISSING_LEDGER"
                filled = current.filled_size if current is not None else ""
                self._maybe_queue_reconciliation_alert(
                    attempt=attempt,
                    result=result,
                    current_state=state,
                    filled_size=filled,
                )
                results.append((attempt.attempt_id, result))
            return tuple(results)

    def _maybe_queue_reconciliation_alert(
        self, *, attempt, result: ReconcileResult, current_state: str, filled_size: str
    ) -> None:
        """Keep routine reconciliation silent and alert only actionable problems."""
        key = attempt.attempt_id
        if result.status != ReconcileStatus.NEEDS_REVIEW:
            self._clear_transient_reconciliation_group(key, notify_recovery=True)
            if self._reconciliation_failures.pop(key, 0):
                self._reconciliation_episodes[key] = (
                    self._reconciliation_episodes.get(key, 0) + 1
                )
            if result.status == ReconcileStatus.CANCELED:
                strategy = self.ledger.get_strategy(attempt.strategy_id)
                if strategy is not None and strategy.state == "ACTIVE":
                    self.ledger.enqueue_notification(
                        dedupe_key=(
                            f"watch-reconcile-active-canceled:{key}:"
                            f"{attempt.updated_at}"
                        ),
                        kind="REVIEW_REQUIRED",
                        message=(
                            f"当前策略挂单意外取消 {attempt.symbol} "
                            f"{attempt.direction}，需要检查"
                        ),
                    )
            return

        critical_states = {
            "CANCELED_PARTIAL",
            "PROTECTION_FAILED",
            "UNKNOWN",
            "CANCEL_UNKNOWN",
            "MISSING_LEDGER",
        }
        if current_state in critical_states:
            self._clear_transient_reconciliation_group(key, notify_recovery=False)
        count = self._reconciliation_failures.get(key, 0) + 1
        self._reconciliation_failures[key] = count
        if current_state not in critical_states and count < 3:
            return
        if current_state not in critical_states:
            self._queue_transient_reconciliation_alert(key, result)
            return
        episode = self._reconciliation_episodes.get(key, 0)
        self.ledger.enqueue_notification(
            dedupe_key=(
                f"watch-reconcile-actionable:{key}:{episode}:"
                f"{current_state}:{filled_size}"
            ),
            kind="REVIEW_REQUIRED",
            message=(
                f"持续对账 {attempt.symbol} {attempt.direction}: "
                f"{result.status.value} {result.reason}"
            ).strip(),
        )

    def _queue_transient_reconciliation_alert(
        self, attempt_id: str, result: ReconcileResult
    ) -> None:
        """Emit one alert for an exchange outage shared by many live orders."""
        group = f"{result.status.value}:{result.reason}"
        previous = self._transient_attempt_groups.get(attempt_id)
        if previous == group:
            return
        if previous is not None:
            self._clear_transient_reconciliation_group(
                attempt_id, notify_recovery=False
            )
        members = self._transient_failure_groups.setdefault(group, set())
        first_member = not members
        members.add(attempt_id)
        self._transient_attempt_groups[attempt_id] = group
        if not first_member:
            return
        episode = self._transient_group_episodes.get(group, 0)
        self.ledger.enqueue_notification(
            dedupe_key=f"watch-reconcile-transient:{group}:{episode}",
            kind="REVIEW_REQUIRED",
            message=(
                f"持续对账暂时不可用：{result.reason}。"
                "同类错误已合并提醒，正在自动重试；"
                "不会据此撤单、下单或修改仓位。"
            ),
        )

    def _clear_transient_reconciliation_group(
        self, attempt_id: str, *, notify_recovery: bool
    ) -> None:
        """Close a shared transient-error episode once every member recovers."""
        group = self._transient_attempt_groups.pop(attempt_id, None)
        if group is None:
            return
        members = self._transient_failure_groups.get(group)
        if members is None:
            return
        members.discard(attempt_id)
        if members:
            return
        del self._transient_failure_groups[group]
        episode = self._transient_group_episodes.get(group, 0)
        if notify_recovery:
            self.ledger.enqueue_notification(
                dedupe_key=f"watch-reconcile-transient-recovered:{group}:{episode}",
                kind="RECONCILIATION",
                message="持续对账连接已恢复，已恢复自动核对现有订单。",
            )
        self._transient_group_episodes[group] = episode + 1

    def recover_attempt(
        self, attempt_id: str
    ) -> tuple[ReconcileResult, ExecutionOutcome | None]:
        with self._operation_lock, ExclusiveProcessLock(self.lock_path):
            reconciliation = self._reconciler().reconcile(attempt_id)
            attempt = self.ledger.get_attempt(attempt_id)
            if attempt is not None:
                self.ledger.enqueue_notification(
                    dedupe_key=(
                        f"reconcile:{attempt_id}:{attempt.state}:"
                        f"{attempt.retry_count}"
                    ),
                    kind=(
                        "REVIEW_REQUIRED"
                        if reconciliation.status == ReconcileStatus.NEEDS_REVIEW
                        else "RECONCILIATION"
                    ),
                    message=(
                        f"订单对账 {attempt.symbol} {attempt.direction}: "
                        f"{reconciliation.status.value} {reconciliation.reason}"
                    ).strip(),
                )
            if reconciliation.status != ReconcileStatus.MISSING_CONFIRMED:
                return reconciliation, None
            if attempt is None:
                return reconciliation, None
            strategy = self.ledger.get_strategy(attempt.strategy_id)
            if strategy is None or strategy.state != "ACTIVE":
                self.ledger.enqueue_notification(
                    dedupe_key=f"retry-superseded:{attempt_id}",
                    kind="RECONCILIATION",
                    message="已确认旧策略订单不存在；因策略已被替换，不再重试",
                )
                return reconciliation, None
            try:
                retry = self.ledger.prepare_confirmed_missing_retry(attempt_id)
            except LedgerConflict as exc:
                self.ledger.enqueue_notification(
                    dedupe_key=f"retry-blocked:{attempt_id}",
                    kind="REVIEW_REQUIRED",
                    message=f"订单已达到自动重试上限或状态变化: {exc}",
                )
                return reconciliation, None
            execution = self._execute_unlocked(retry.strategy_id)
            return reconciliation, execution

    def _execute_unlocked(self, strategy_id: str) -> ExecutionOutcome:
        pause_reason = self.ledger.automation_pause_reason()
        if pause_reason:
            return self._queue_blocked_execution(
                strategy_id,
                f"自动化已暂停：{pause_reason}；保留现有订单和仓位",
            )
        if (
            self.require_source_attestation
            and not self.ledger.strategy_has_authenticated_ready_ingest(strategy_id)
        ):
            return self._queue_blocked_execution(
                strategy_id,
                "策略缺少已认证 Telegram READY 来源审计，拒绝执行",
            )
        blockers = self.ledger.list_superseded_unresolved_attempts(strategy_id)
        for blocker in blockers:
            # FILLED is the entry's execution state, not proof of a current
            # position. Reconcile its protection history and position first.
            reconciliation = self._reconciler().reconcile(blocker.attempt_id)
            if reconciliation.retryable and not self.ledger.automation_pause_reason():
                return ExecutionOutcome(ExecutionStatus.RETRYABLE,
                    reason=f"旧策略对账暂时不可用: {blocker.attempt_id} {reconciliation.reason}")
            if reconciliation.status not in {
                ReconcileStatus.LIVE,
                ReconcileStatus.CANCELED,
                ReconcileStatus.CLOSED,
                ReconcileStatus.MISSING_CONFIRMED,
            }:
                return self._queue_blocked_execution(
                    strategy_id,
                    f"旧策略对账未闭环: {blocker.attempt_id} {reconciliation.reason}",
                )
        if self.ledger.automation_pause_reason():
            return self._queue_blocked_execution(strategy_id, "对账发现保护异常，自动化已暂停")
        attempts = self.ledger.list_attempts(strategy_id)
        signals = tuple(
            signal
            for attempt in attempts
            if (signal := self.ledger.get_attempt_signal(attempt.attempt_id)) is not None
        )
        if len(signals) != len(attempts) or not signals:
            outcome = ExecutionOutcome(
                ExecutionStatus.NEEDS_REVIEW,
                reason="账本缺少完整的权威策略参数",
            )
        else:
            outcome = Executor(
                ledger=self.ledger, gateway=self.gateway
            ).execute_strategy(strategy_id, signals)
        state_key = ",".join(
            f"{item.direction}-{item.state}-r{item.retry_count}"
            for item in self.ledger.list_attempts(strategy_id)
        )
        kind = (
            "EXECUTION"
            if outcome.status == ExecutionStatus.SUBMITTED
            else "REVIEW_REQUIRED"
        )
        if self.ledger.get_execution_job(strategy_id) is not None:
            return outcome
        self.ledger.enqueue_notification(
            dedupe_key=f"execution:{strategy_id}:{state_key}",
            kind=kind,
            message=(
                f"策略执行 {strategy_id}: {outcome.status.value}; "
                f"下单数={outcome.placed}; {outcome.reason}"
            ).strip(),
        )
        return outcome

    def _queue_blocked_execution(
        self, strategy_id: str, reason: str
    ) -> ExecutionOutcome:
        outcome = ExecutionOutcome(ExecutionStatus.NEEDS_REVIEW, reason=reason)
        self.ledger.enqueue_notification(
            dedupe_key=f"execution-blocked:{strategy_id}:{reason}",
            kind="REVIEW_REQUIRED",
            message=reason,
        )
        return outcome
