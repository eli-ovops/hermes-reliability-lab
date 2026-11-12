"""Codex 审查反例回归测试：等待/观察语义、v1 升级后可写、ticker 紧贴中文。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src_v2.ledger import Ledger
from src_v2.parser import DecisionKind, TradeSignal, analyze_message


class WaitingSemanticsTests(unittest.TestCase):
    def test_wait_for_pullback_not_ready(self):
        d = analyze_message("BTC 65000做多，止损64000，止盈67000，等待回踩确认")
        self.assertEqual(DecisionKind.NEEDS_REVIEW, d.kind)
        self.assertEqual(0, len(d.signals))

    def test_observe_then_low_absorb_not_ready(self):
        d = analyze_message("SUI 观察0.79附近支撑，等企稳再低吸，止损0.75，止盈0.90")
        self.assertEqual(DecisionKind.NEEDS_REVIEW, d.kind)
        self.assertEqual(0, len(d.signals))

    def test_watch_only_not_ready(self):
        d = analyze_message("BCH 先观望，等回踩250附近再买入")
        self.assertEqual(DecisionKind.NEEDS_REVIEW, d.kind)
        self.assertEqual(0, len(d.signals))


class V1UpgradeWritesTests(unittest.TestCase):
    def _build_v1_db(self, path: Path) -> None:
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
        # v1 旧表缺 entry_low/entry_high/entry_price/stop_price/target_price 列
        conn.execute(
            "CREATE TABLE direction_attempts ("
            "attempt_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL,"
            "symbol TEXT NOT NULL, direction TEXT NOT NULL, state TEXT NOT NULL,"
            "cl_ord_id TEXT NOT NULL UNIQUE, ord_id TEXT, requested_size TEXT,"
            "filled_size TEXT NOT NULL DEFAULT '0', retry_count INTEGER NOT NULL DEFAULT 0,"
            "last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
            "UNIQUE(strategy_id, direction))"
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
        conn.commit()
        conn.close()

    def test_v1_upgrade_then_record_new_strategy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v1.sqlite3"
            self._build_v1_db(path)
            ledger = Ledger(path)  # 触发 migration

            signal = TradeSignal(
                symbol="SUI-USDT-SWAP",
                direction="long",
                entry_low=0.78,
                entry_high=0.79,
                entry_price=0.785,
                stop=0.75,
                target=0.90,
            )
            ledger.record_strategy(
                strategy_id="new",
                symbol=signal.symbol,
                source_chat_id=-100,
                source_sender_id=5356537814,
                source_message_id=2,
                raw_text="SUI 0.79-0.78低吸，止损0.75，止盈0.90",
                directions=("long",),
                signals=(signal,),
            )
            attempt = ledger.list_attempts("new")[0]
            signal = ledger.get_attempt_signal(attempt.attempt_id)
            self.assertEqual(0.75, signal.stop)
            self.assertEqual(0.90, signal.target)


class TickerAdjacentChineseTests(unittest.TestCase):
    def test_sui_adjacent_chinese(self):
        d = analyze_message("SUI这种结构，0.79-0.78低吸，止损0.75，止盈0.90")
        self.assertEqual(DecisionKind.READY, d.kind)
        self.assertEqual("SUI-USDT-SWAP", d.signals[0].symbol)

    def test_bch_adjacent_chinese(self):
        d = analyze_message("看好BCH，255-250回踩买入，止损245，止盈270")
        self.assertEqual(DecisionKind.READY, d.kind)
        self.assertEqual("BCH-USDT-SWAP", d.signals[0].symbol)


class CodexFollowupSemanticsTests(unittest.TestCase):
    def test_stand_then_enter_not_ready(self):
        d = analyze_message(
            "SUI 0.79附近做多，如果后续站稳再进，止损0.75，止盈0.90"
        )
        self.assertEqual(DecisionKind.NEEDS_REVIEW, d.kind)
        self.assertEqual(0, len(d.signals))

    def test_not_stand_do_not_enter_not_ready(self):
        d = analyze_message(
            "SUI 0.79附近做多，未站稳前不要进，止损0.75，止盈0.90"
        )
        self.assertEqual(DecisionKind.NEEDS_REVIEW, d.kind)
        self.assertEqual(0, len(d.signals))

    def test_watch_low_absorb_not_ready(self):
        d = analyze_message("SUI 关注0.79低吸，止损0.75，止盈0.90")
        self.assertEqual(DecisionKind.NEEDS_REVIEW, d.kind)
        self.assertEqual(0, len(d.signals))


class CodexSecondRoundTests(unittest.TestCase):
    def test_resistance_price_not_taken_as_entry(self):
        d = analyze_message("ETH关注2400附近阻力，2290做多，止损2260，止盈2600")
        self.assertEqual(DecisionKind.READY, d.kind)
        self.assertEqual(2290.0, d.signals[0].entry_price)
        self.assertEqual(2260.0, d.signals[0].stop)
        self.assertEqual(2600.0, d.signals[0].target)

    def test_observed_breakout_is_market_description(self):
        d = analyze_message("ETH观察到放量突破，2290做多，止损2260，止盈2350")
        self.assertEqual(DecisionKind.READY, d.kind)
        self.assertEqual(2290.0, d.signals[0].entry_price)


class TypoSlTpTests(unittest.TestCase):
    def test_duplicate_stop_label_requires_review_without_inventing_target(self):
        d = analyze_message("ETH 靠近2510-2525区域逢高做空，止损2540，止损2420-2400")
        self.assertEqual(DecisionKind.NEEDS_REVIEW, d.kind)
        self.assertEqual((), d.signals)

    def test_btc_long_dual_strategy_normal(self):
        d = analyze_message(
            "BTC 靠近76000-75500区域逢低做多，止损75000，止盈78000-78500"
        )
        self.assertEqual(DecisionKind.READY, d.kind)
        s = d.signals[0]
        self.assertAlmostEqual(75750.0, s.entry_price)
        self.assertAlmostEqual(75000.0, s.stop)
        self.assertAlmostEqual(78000.0, s.target)


if __name__ == "__main__":
    unittest.main()
