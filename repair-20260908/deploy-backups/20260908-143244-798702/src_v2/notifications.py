from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from src_v2.ledger import Ledger


class NotificationDispatcher:
    """把账本通知 outbox 投递给外部发送器，带指数退避限流。

    失败的通知保留 PENDING，只有在退避窗口过期后才重试，
    避免微信通道每轮重复轰炸。
    """

    def __init__(self, *, ledger: Ledger):
        self.ledger = ledger

    def flush(
        self,
        sender: Callable[[str], object],
        *,
        limit: int = 100,
        now: datetime | None = None,
        backoff_base: float = 0.0,
        max_backoff: float = 3600.0,
        pause_threshold: int = 3,
        max_attempts: int | None = None,
    ) -> tuple[int, int]:
        now = now or datetime.now(timezone.utc)
        sent = 0
        failed = 0
        for notification in self.ledger.list_pending_notifications(limit=limit):
            if not self._due(
                notification.attempt_count,
                notification.updated_at,
                now,
                backoff_base,
                max_backoff,
            ):
                continue
            if max_attempts is not None and sent + failed >= max_attempts:
                break
            try:
                sender(notification.message)
            except Exception as exc:
                self.ledger.mark_notification_failed(
                    notification.notification_id,
                    f"{type(exc).__name__}: {exc}",
                )
                failed += 1
                continue
            self.ledger.mark_notification_sent(notification.notification_id)
            sent += 1
        return sent, failed

    @staticmethod
    def _due(
        attempt_count: int,
        updated_at: str,
        now: datetime,
        backoff_base: float,
        max_backoff: float,
    ) -> bool:
        if attempt_count <= 0:
            return True
        try:
            last = datetime.fromisoformat(updated_at)
        except ValueError:
            return True
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        delay = min(backoff_base * (2 ** (attempt_count - 1)), max_backoff)
        return now - last >= timedelta(seconds=delay)
