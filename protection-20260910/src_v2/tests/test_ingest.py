import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src_v2.ingest import IngestStatus, Intake, SourceEnvelope
from src_v2.ledger import Ledger


class IntakeBehaviorTests(unittest.TestCase):
    def test_username_match_cannot_bypass_numeric_sender_and_chat_allowlists(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            intake = Intake(
                ledger=ledger,
                allowed_chat_ids={-100},
                allowed_sender_ids={1818},
            )
            forged = SourceEnvelope(
                chat_id=-999,
                sender_id=9999,
                sender_username="bangzhu1818",
                message_id=77,
                message_date=datetime.now(timezone.utc),
                text="BTC 65000做多，止损64000，止盈67000",
            )

            result = intake.ingest(forged)

            self.assertEqual(IngestStatus.REJECTED_SOURCE, result.status)
            self.assertIsNone(ledger.get_active_strategy("BTC-USDT-SWAP"))

    def test_authenticated_complete_message_is_durably_retrievable_for_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            intake = Intake(
                ledger=ledger,
                allowed_chat_ids={-100},
                allowed_sender_ids={1818},
            )
            envelope = SourceEnvelope(
                chat_id=-100,
                sender_id=1818,
                sender_username="anything",
                message_id=77,
                message_date=datetime.now(timezone.utc),
                text="BTC 65000做多，止损64000，止盈67000",
            )

            result = intake.ingest(envelope)

            self.assertEqual(IngestStatus.RECORDED, result.status)
            attempt = ledger.list_attempts(result.strategy_id)[0]
            signal = ledger.get_attempt_signal(attempt.attempt_id)
            self.assertEqual(65000.0, signal.entry_price)
            self.assertEqual(64000.0, signal.stop)
            self.assertEqual(67000.0, signal.target)

    def test_edited_message_is_audited_without_pausing_or_notifying(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            intake = Intake(
                ledger=ledger,
                allowed_chat_ids={-100},
                allowed_sender_ids={1818},
            )
            envelope = SourceEnvelope(
                chat_id=-100,
                sender_id=1818,
                sender_username="trusted",
                message_id=7,
                message_date=datetime.now(timezone.utc),
                text="BTC 65000做多，止损64000，止盈67000",
                event_kind="edited",
            )

            result = intake.ingest(envelope)

            self.assertEqual(IngestStatus.IGNORED, result.status)
            self.assertIsNone(result.strategy_id)
            self.assertIsNone(ledger.get_active_strategy("BTC-USDT-SWAP"))
            self.assertIsNone(ledger.automation_pause_reason())
            self.assertEqual((), ledger.list_pending_notifications())
            audit = ledger.list_ingest_events()
            self.assertEqual(1, len(audit))
            self.assertEqual("edited", audit[0].event_kind)
            self.assertEqual("IGNORE", audit[0].decision_kind)

    def test_edited_message_does_not_block_a_later_new_strategy(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            intake = Intake(
                ledger=ledger,
                allowed_chat_ids={-100},
                allowed_sender_ids={1818},
            )
            intake.ingest(
                SourceEnvelope(
                    chat_id=-100,
                    sender_id=1818,
                    sender_username="trusted",
                    message_id=7,
                    message_date=datetime.now(timezone.utc),
                    text="BTC 65000做多，止损64000，止盈67000",
                    event_kind="edited",
                )
            )

            later = intake.ingest(
                SourceEnvelope(
                    chat_id=-100,
                    sender_id=1818,
                    sender_username="trusted",
                    message_id=8,
                    message_date=datetime.now(timezone.utc),
                    text="BTC 65000做多，止损64000，止盈67000",
                )
            )

            self.assertEqual(IngestStatus.RECORDED, later.status)
            self.assertIsNotNone(later.strategy_id)
            self.assertIsNotNone(ledger.get_active_strategy("BTC-USDT-SWAP"))
            self.assertIsNone(ledger.automation_pause_reason())
            self.assertEqual(0, len(ledger.list_pending_notifications()))
            self.assertEqual(2, len(ledger.list_ingest_events()))

    def test_authenticated_strategy_older_than_five_minutes_never_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            now = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
            intake = Intake(
                ledger=ledger,
                allowed_chat_ids={-100},
                allowed_sender_ids={1818},
                now_provider=lambda: now,
            )

            result = intake.ingest(
                SourceEnvelope(
                    chat_id=-100,
                    sender_id=1818,
                    sender_username="trusted",
                    message_id=9,
                    message_date=now - timedelta(minutes=5, seconds=1),
                    text="BTC 65000做多，止损64000，止盈67000",
                )
            )

            self.assertEqual(IngestStatus.NEEDS_REVIEW, result.status)
            self.assertIsNone(result.strategy_id)
            self.assertIn("超过5分钟", result.decision.questions[0])
            self.assertIsNone(ledger.get_active_strategy("BTC-USDT-SWAP"))

    def test_partially_complete_dual_strategy_never_executes_one_side(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            intake = Intake(
                ledger=ledger,
                allowed_chat_ids={-100},
                allowed_sender_ids={1818},
            )

            result = intake.ingest(
                SourceEnvelope(
                    chat_id=-100,
                    sender_id=1818,
                    sender_username="trusted",
                    message_id=10,
                    message_date=datetime.now(timezone.utc),
                    text=(
                        "BTC 64000做多，止损63000，止盈65000；"
                        "66000做空，等待确认"
                    ),
                )
            )

            self.assertEqual(IngestStatus.NEEDS_REVIEW, result.status)
            self.assertIsNone(result.strategy_id)
            self.assertIsNone(ledger.get_active_strategy("BTC-USDT-SWAP"))


if __name__ == "__main__":
    unittest.main()
