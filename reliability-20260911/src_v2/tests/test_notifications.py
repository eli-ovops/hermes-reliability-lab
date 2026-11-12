import tempfile
import unittest
from pathlib import Path

from src_v2.ledger import Ledger
from src_v2.notifications import NotificationDispatcher


class NotificationBehaviorTests(unittest.TestCase):
    def test_delivery_failure_keeps_notification_pending_for_later_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            ledger.enqueue_notification(
                dedupe_key="review:1",
                kind="REVIEW_REQUIRED",
                message="需要确认策略",
            )
            dispatcher = NotificationDispatcher(ledger=ledger)

            first = dispatcher.flush(
                lambda message: (_ for _ in ()).throw(ConnectionError("wechat down"))
            )

            self.assertEqual((0, 1), first)
            pending = ledger.list_pending_notifications()
            self.assertEqual(1, len(pending))
            self.assertEqual(1, pending[0].attempt_count)

            sent_messages = []
            second = dispatcher.flush(sent_messages.append)

            self.assertEqual((1, 0), second)
            self.assertEqual(["需要确认策略"], sent_messages)
            self.assertEqual((), ledger.list_pending_notifications())

    def test_same_event_cannot_create_duplicate_notifications(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")

            first = ledger.enqueue_notification(
                dedupe_key="execution:one",
                kind="EXECUTION",
                message="submitted",
            )
            second = ledger.enqueue_notification(
                dedupe_key="execution:one",
                kind="EXECUTION",
                message="submitted again",
            )

            self.assertEqual(first.notification_id, second.notification_id)
            self.assertEqual(1, len(ledger.list_pending_notifications()))

    def test_three_delivery_failures_do_not_pause_new_automation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            ledger.enqueue_notification(
                dedupe_key="critical-1",
                kind="REVIEW_REQUIRED",
                message="需要用户确认",
            )
            dispatcher = NotificationDispatcher(ledger=ledger)

            def unavailable(_message):
                raise TimeoutError("wechat unavailable")

            for _ in range(3):
                dispatcher.flush(unavailable, backoff_base=0)

            self.assertIsNone(ledger.automation_pause_reason())
            pending = ledger.list_pending_notifications()
            self.assertEqual(1, len(pending))
            self.assertEqual(3, pending[0].attempt_count)

    def test_failed_notification_not_retried_before_backoff_expires(self):
        from datetime import datetime, timedelta, timezone

        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            ledger.enqueue_notification(
                dedupe_key="review:backoff",
                kind="REVIEW_REQUIRED",
                message="需要确认",
            )
            dispatcher = NotificationDispatcher(ledger=ledger)

            def fail(_):
                raise ConnectionError("wechat down")

            t0 = datetime.now(timezone.utc)
            dispatcher.flush(fail, now=t0, backoff_base=60.0)
            pending = ledger.list_pending_notifications()
            self.assertEqual(1, pending[0].attempt_count)

            # 30 秒后仍小于 60 秒退避窗口，不得重试
            dispatcher.flush(fail, now=t0 + timedelta(seconds=30), backoff_base=60.0)
            pending = ledger.list_pending_notifications()
            self.assertEqual(1, pending[0].attempt_count)

    def test_failed_notification_retried_after_backoff_expires(self):
        from datetime import datetime, timedelta, timezone

        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            ledger.enqueue_notification(
                dedupe_key="review:retry",
                kind="REVIEW_REQUIRED",
                message="需要确认",
            )
            dispatcher = NotificationDispatcher(ledger=ledger)

            def fail(_):
                raise ConnectionError("wechat down")

            sent = []
            t0 = datetime.now(timezone.utc)
            dispatcher.flush(fail, now=t0, backoff_base=60.0)
            # 61 秒后超过 60 秒窗口，重试成功
            dispatcher.flush(sent.append, now=t0 + timedelta(seconds=61), backoff_base=60.0)
            self.assertEqual(["需要确认"], sent)
            self.assertEqual((), ledger.list_pending_notifications())


if __name__ == "__main__":
    unittest.main()
