import tempfile
import unittest
from pathlib import Path

from src_v2.ledger import Ledger, LedgerConflict


class LedgerBehaviorTests(unittest.TestCase):
    def test_account_binding_is_idempotent_and_cannot_be_reassigned(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")

            ledger.bind_account("a" * 64, reason="initial binding")
            ledger.bind_account("a" * 64, reason="same account restart")

            self.assertEqual("a" * 64, ledger.account_fingerprint())
            with self.assertRaisesRegex(LedgerConflict, "其他 OKX 账号"):
                ledger.bind_account("b" * 64, reason="wrong account")

    def test_recorded_strategy_is_retrievable_with_one_attempt_per_direction(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")

            ledger.record_strategy(
                strategy_id="tg--100-77",
                symbol="BTC-USDT-SWAP",
                source_chat_id=-100,
                source_sender_id=1818,
                source_message_id=77,
                raw_text="BTC 双向策略",
                directions=("long", "short"),
            )

            strategy = ledger.get_active_strategy("BTC-USDT-SWAP")
            self.assertEqual("tg--100-77", strategy.strategy_id)
            self.assertEqual(
                ("long", "short"),
                tuple(item.direction for item in ledger.list_attempts(strategy.strategy_id)),
            )

    def test_only_a_ledger_owned_superseded_bangzhu_entry_is_cancellable(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            ledger.record_strategy(
                strategy_id="old",
                symbol="BTC-USDT-SWAP",
                source_chat_id=-100,
                source_sender_id=1818,
                source_message_id=1,
                raw_text="old",
                directions=("long",),
            )
            old_attempt = ledger.list_attempts("old")[0]
            ledger.transition_attempt(
                old_attempt.attempt_id,
                expected_states=("READY",),
                new_state="LIVE",
                ord_id="OKX-OLD",
                requested_size="1",
            )
            ledger.record_strategy(
                strategy_id="new",
                symbol="BTC-USDT-SWAP",
                source_chat_id=-100,
                source_sender_id=1818,
                source_message_id=2,
                raw_text="new",
                directions=("long",),
            )

            owned = {
                "instId": "BTC-USDT-SWAP",
                "ordId": "OKX-OLD",
                "clOrdId": old_attempt.cl_ord_id,
                "tag": "BANGZHU",
                "ordType": "limit",
                "state": "live",
            }
            manual = dict(owned, ordId="MANUAL", clOrdId="CLI-MANUAL", tag="CLI")
            partial = dict(owned, state="partially_filled")

            self.assertTrue(ledger.is_cancellable_old_entry(owned, replacing_strategy_id="new"))
            self.assertFalse(ledger.is_cancellable_old_entry(manual, replacing_strategy_id="new"))
            self.assertFalse(
                ledger.is_cancellable_old_entry(partial, replacing_strategy_id="new")
            )

    def test_confirmed_missing_order_can_be_retried_at_most_three_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            ledger.record_strategy(
                strategy_id="new",
                symbol="BTC-USDT-SWAP",
                source_chat_id=-100,
                source_sender_id=1818,
                source_message_id=2,
                raw_text="new",
                directions=("long",),
            )
            attempt = ledger.list_attempts("new")[0]
            for expected_count in (1, 2, 3):
                ledger.transition_attempt(
                    attempt.attempt_id,
                    expected_states=("READY",),
                    new_state="MISSING_CONFIRMED",
                )
                retried = ledger.prepare_confirmed_missing_retry(attempt.attempt_id)
                self.assertEqual(expected_count, retried.retry_count)
            ledger.transition_attempt(
                attempt.attempt_id,
                expected_states=("READY",),
                new_state="MISSING_CONFIRMED",
            )

            with self.assertRaises(Exception):
                ledger.prepare_confirmed_missing_retry(attempt.attempt_id)


if __name__ == "__main__":
    unittest.main()
