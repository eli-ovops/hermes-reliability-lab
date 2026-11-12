"""intake 审计（ingest_events 表）测试 — 阶段：先失败。"""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src_v2.ingest import Intake, SourceEnvelope
from src_v2.ledger import Ledger


CHAT = -1002069530742
SENDER = 5356537814


class IngestAuditTests(unittest.TestCase):
    def _intake(self, tmp):
        ledger = Ledger(Path(tmp) / "ledger.sqlite3")
        return Intake(
            ledger=ledger,
            allowed_chat_ids={CHAT},
            allowed_sender_ids={SENDER},
        ), ledger

    def _env(self, text, message_id=1, event_kind="new", chat=CHAT, sender=SENDER):
        return SourceEnvelope(
            chat_id=chat,
            sender_id=sender,
            sender_username="bangzhu1818",
            message_id=message_id,
            message_date=datetime.now(timezone.utc),
            text=text,
            event_kind=event_kind,
        )

    def test_authenticated_complete_message_recorded_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            intake, ledger = self._intake(tmp)
            intake.ingest(self._env("SUI 0.79-0.78区域低吸，止损0.75，止盈0.90"))
            events = ledger.list_ingest_events()
            self.assertEqual(1, len(events))
            self.assertEqual("SUI 0.79-0.78区域低吸，止损0.75，止盈0.90", events[0].raw_text)

    def test_needs_review_saves_raw_text_and_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            intake, ledger = self._intake(tmp)
            intake.ingest(self._env("SUI 关注突破后回踩，如果撑住这里买点共振"))
            events = ledger.list_ingest_events()
            self.assertEqual(1, len(events))
            self.assertIn("条件", events[0].reasons)

    def test_duplicate_event_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            intake, ledger = self._intake(tmp)
            env = self._env("SUI 0.79-0.78区域低吸，止损0.75，止盈0.90", message_id=7)
            intake.ingest(env)
            intake.ingest(env)
            self.assertEqual(1, len(ledger.list_ingest_events()))

    def test_wrong_chat_or_sender_not_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            intake, ledger = self._intake(tmp)
            intake.ingest(self._env("SUI 0.79低吸，止损0.75，止盈0.90", chat=-999999))
            intake.ingest(self._env("SUI 0.79低吸，止损0.75，止盈0.90", sender=999999))
            self.assertEqual(0, len(ledger.list_ingest_events()))

    def test_old_schema_upgrades_without_data_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite3"
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA user_version=1")
            conn.execute(
                "CREATE TABLE strategy_versions ("
                "strategy_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, state TEXT NOT NULL,"
                "source_chat_id INTEGER NOT NULL, source_sender_id INTEGER NOT NULL,"
                "source_message_id INTEGER NOT NULL, raw_text TEXT NOT NULL,"
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
                "UNIQUE(source_chat_id, source_message_id))"
            )
            conn.execute(
                "CREATE TABLE direction_attempts ("
                "attempt_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL,"
                "symbol TEXT NOT NULL, direction TEXT NOT NULL, state TEXT NOT NULL,"
                "cl_ord_id TEXT NOT NULL UNIQUE, ord_id TEXT, requested_size TEXT,"
                "filled_size TEXT NOT NULL DEFAULT '0', retry_count INTEGER NOT NULL DEFAULT 0,"
                "last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE protections (attempt_id TEXT PRIMARY KEY, algo_id TEXT NOT NULL UNIQUE,"
                "covered_size TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,"
                "updated_at TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE notifications (notification_id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "dedupe_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, message TEXT NOT NULL,"
                "state TEXT NOT NULL DEFAULT 'PENDING', attempt_count INTEGER NOT NULL DEFAULT 0,"
                "last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO strategy_versions VALUES ('old', 'BTC-USDT-SWAP', 'ACTIVE', -1, 1, 1,"
                "'legacy', 't', 't')"
            )
            conn.commit()
            conn.close()

            ledger = Ledger(path)  # 触发 migration

            # 旧数据保留
            self.assertIsNotNone(ledger.get_strategy("old"))
            # ingest_events 表已创建
            events = ledger.list_ingest_events()
            self.assertEqual(0, len(events))
            upgraded = sqlite3.connect(path)
            try:
                self.assertEqual(
                    4, upgraded.execute("PRAGMA user_version").fetchone()[0]
                )
                tables = {
                    row[0]
                    for row in upgraded.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            finally:
                upgraded.close()
            self.assertIn("runtime_controls", tables)
            self.assertIn("order_plan_snapshots", tables)


if __name__ == "__main__":
    unittest.main()
