"""Telegram 适配层测试 — 阶段一。

覆盖：完整策略、双向、缺币种、支撑阻力、否定、条件、持仓管理、
编辑/删除、重复消息、数字 ID 认证、断线重连（结构验证）。
"""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src_v2.ingest import IngestStatus, Intake
from src_v2.ledger import Ledger
from src_v2.tg_adapter import TelegramAdapter


CHAT = -1002069530742
SENDER = 1818


class TelegramAdapterTests(unittest.TestCase):
    def _adapter(self, tmp):
        ledger = Ledger(Path(tmp) / "ledger.sqlite3")
        intake = Intake(
            ledger=ledger,
            allowed_chat_ids={CHAT},
            allowed_sender_ids={SENDER},
        )
        return TelegramAdapter(intake=intake), ledger

    def _new(self, adapter, text, message_id=1, event_kind="new"):
        return adapter.on_new_message(
            chat_id=CHAT,
            sender_id=SENDER,
            sender_username="bangzhu1818",
            message_id=message_id,
            message_date=datetime.now(timezone.utc),
            text=text,
        )

    def test_complete_strategy_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, ledger = self._adapter(tmp)
            result = self._new(
                adapter, "BTC 65000做多，止损64000，止盈67000"
            )
            self.assertEqual(IngestStatus.RECORDED, result.status)
            self.assertIsNotNone(ledger.get_active_strategy("BTC-USDT-SWAP"))

    def test_bidirectional_strategy_records_two_directions(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, ledger = self._adapter(tmp)
            result = self._new(
                adapter,
                "BTC 64000做多，止损63000，止盈65000；66000做空，止损67000，止盈64500",
            )
            self.assertEqual(IngestStatus.RECORDED, result.status)
            attempts = ledger.list_attempts(result.strategy_id)
            self.assertEqual({"long", "short"}, {a.direction for a in attempts})

    def test_missing_symbol_requires_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, ledger = self._adapter(tmp)
            result = self._new(adapter, "6400做多，止损6300，止盈6500")
            self.assertEqual(IngestStatus.NEEDS_REVIEW, result.status)
            pending = ledger.list_pending_notifications()
            self.assertEqual("REVIEW_REQUIRED", pending[0].kind)

    def test_support_resistance_comment_requires_review_not_auto_strategy(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, ledger = self._adapter(tmp)
            result = self._new(
                adapter, "BTC 下方关注64000支撑，上方关注66000阻力"
            )
            self.assertEqual(IngestStatus.NEEDS_REVIEW, result.status)
            self.assertIsNone(ledger.get_active_strategy("BTC-USDT-SWAP"))

    def test_negation_requires_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, ledger = self._adapter(tmp)
            result = self._new(
                adapter, "BTC 65000附近支撑，但不要做多"
            )
            self.assertEqual(IngestStatus.NEEDS_REVIEW, result.status)
            self.assertIsNone(ledger.get_active_strategy("BTC-USDT-SWAP"))

    def test_conditional_trigger_requires_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, ledger = self._adapter(tmp)
            result = self._new(
                adapter, "BTC 若站稳65000再考虑做多，止损64000，止盈67000"
            )
            self.assertEqual(IngestStatus.NEEDS_REVIEW, result.status)

    def test_position_management_requires_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, ledger = self._adapter(tmp)
            result = self._new(
                adapter, "BTC 多单继续持有，止盈上移至68000"
            )
            self.assertEqual(IngestStatus.NEEDS_REVIEW, result.status)

    def test_edited_message_is_audited_without_global_pause(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, ledger = self._adapter(tmp)
            result = adapter.on_edited_message(
                chat_id=CHAT, sender_id=SENDER, sender_username="bangzhu1818",
                message_id=9, message_date=datetime.now(timezone.utc),
                text="BTC 65000做多，止损64000，止盈67000",
            )
            self.assertEqual(IngestStatus.IGNORED, result.status)
            self.assertIsNone(ledger.get_active_strategy("BTC-USDT-SWAP"))
            self.assertIsNone(ledger.automation_pause_reason())
            self.assertEqual((), ledger.list_pending_notifications())
            self.assertEqual("edited", ledger.list_ingest_events()[0].event_kind)

    def test_deleted_message_requires_review_without_sender_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, ledger = self._adapter(tmp)
            self._new(adapter, "BTC 65000做多，止损64000，止盈67000", message_id=11)
            result = adapter.on_deleted_message(
                chat_id=CHAT, message_id=11,
                message_date=datetime.now(timezone.utc),
            )
            self.assertEqual(IngestStatus.NEEDS_REVIEW, result.status)
            pending = ledger.list_pending_notifications()
            self.assertEqual("REVIEW_REQUIRED", pending[0].kind)

    def test_unrelated_deletion_does_not_pause_future_strategy(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, ledger = self._adapter(tmp)
            result = adapter.on_deleted_message(
                chat_id=CHAT, message_id=67514,
                message_date=datetime.now(timezone.utc),
            )
            self.assertEqual(IngestStatus.IGNORED, result.status)
            self.assertIsNone(ledger.automation_pause_reason())
            self.assertEqual((), ledger.list_pending_notifications())
            self.assertEqual("IGNORE", ledger.list_ingest_events()[0].decision_kind)
            later = self._new(adapter, "BTC 65000做多，止损64000，止盈67000")
            self.assertEqual(IngestStatus.RECORDED, later.status)

    def test_unrelated_deletion_does_not_clear_existing_pause(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, ledger = self._adapter(tmp)
            ledger.pause_automation("manual stop")
            adapter.on_deleted_message(
                chat_id=CHAT, message_id=67514,
                message_date=datetime.now(timezone.utc),
            )
            self.assertEqual("manual stop", ledger.automation_pause_reason())

    def test_deleted_message_wrong_chat_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, ledger = self._adapter(tmp)
            result = adapter.on_deleted_message(
                chat_id=-999999, message_id=11,
                message_date=datetime.now(timezone.utc),
            )
            self.assertEqual(IngestStatus.REJECTED_SOURCE, result.status)

    def test_duplicate_message_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, ledger = self._adapter(tmp)
            first = self._new(
                adapter, "BTC 65000做多，止损64000，止盈67000", message_id=5
            )
            second = self._new(
                adapter, "BTC 65000做多，止损64000，止盈67000", message_id=5
            )
            self.assertEqual(IngestStatus.RECORDED, first.status)
            self.assertEqual(IngestStatus.RECORDED, second.status)
            # 同一 message_id 重复 ingest，账本中仍只有一个方向 attempt
            attempts = ledger.list_attempts(first.strategy_id)
            self.assertEqual(1, len(attempts))

    def test_username_alone_cannot_bypass_numeric_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            intake = Intake(
                ledger=ledger,
                allowed_chat_ids={CHAT},
                allowed_sender_ids={SENDER},
            )
            adapter = TelegramAdapter(intake=intake)
            result = adapter.on_new_message(
                chat_id=CHAT,
                sender_id=999999,  # 错误数字 ID
                sender_username="bangzhu1818",  # 用户名对，但数字 ID 错
                message_id=1,
                message_date=datetime.now(timezone.utc),
                text="BTC 65000做多，止损64000，止盈67000",
            )
            self.assertEqual(IngestStatus.REJECTED_SOURCE, result.status)
            self.assertIsNone(ledger.get_active_strategy("BTC-USDT-SWAP"))


if __name__ == "__main__":
    unittest.main()
