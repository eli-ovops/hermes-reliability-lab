import tempfile
import unittest
from pathlib import Path

from src_v2.ledger import Ledger
from src_v2.parser import TradeSignal
from src_v2.protection import ProtectionManager, ProtectionStatus


class FakeProtectionGateway:
    def instrument(self, symbol):
        return {"tickSz": "0.1"}

    def __init__(self):
        self.placed = []
        self.amended = []
        self.cancelled_entries = []
        self.active_algos = []

    def place_protection(self, payload):
        self.placed.append(payload)
        return {"code": "0", "data": [{"algoId": "ALGO-1", "sCode": "0"}]}

    def cancel_entry(self, symbol, ord_id):
        self.cancelled_entries.append((symbol, ord_id))
        return {"code": "0", "data": [{"ordId": ord_id, "sCode": "0"}]}

    def amend_protection(self, symbol, algo_id, new_size):
        self.amended.append((symbol, algo_id, new_size))
        return {"code": "0", "data": [{"algoId": algo_id, "sCode": "0"}]}

    def order(self, symbol, *, ord_id=None, cl_ord_id=None):
        return {
            "code": "0",
            "data": [{
                "instId": symbol,
                "ordId": ord_id,
                "clOrdId": cl_ord_id,
                "state": "canceled",
                "accFillSz": "3",
            }],
        }

    def algo_orders_response(self, symbol):
        return {"code": "0", "data": list(self.active_algos)}


class ProtectionBehaviorTests(unittest.TestCase):
    def test_partial_fill_receives_market_tp_sl_covering_the_filled_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            signal = TradeSignal(
                symbol="BTC-USDT-SWAP",
                direction="long",
                entry_low=65000,
                entry_high=65000,
                entry_price=65000,
                stop=64000,
                target=67000,
            )
            ledger.record_strategy(
                strategy_id="new",
                symbol=signal.symbol,
                source_chat_id=-100,
                source_sender_id=1818,
                source_message_id=2,
                raw_text="new",
                directions=("long",),
                signals=(signal,),
            )
            attempt = ledger.list_attempts("new")[0]
            ledger.transition_attempt(
                attempt.attempt_id,
                expected_states=("READY",),
                new_state="PARTIALLY_FILLED",
                ord_id="ENTRY-1",
                requested_size="10",
                filled_size="3",
            )
            gateway = FakeProtectionGateway()

            result = ProtectionManager(ledger=ledger, gateway=gateway).ensure_coverage(
                attempt.attempt_id
            )

            self.assertEqual(ProtectionStatus.ACTIVE, result.status)
            self.assertEqual("3", gateway.placed[0]["sz"])
            self.assertEqual("-1", gateway.placed[0]["tpOrdPx"])
            self.assertEqual("-1", gateway.placed[0]["slOrdPx"])
            self.assertEqual("3", ledger.get_protection(attempt.attempt_id).covered_size)

    def test_failed_protection_cancels_the_unfilled_entry_remainder(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            signal = TradeSignal(
                symbol="BTC-USDT-SWAP",
                direction="long",
                entry_low=65000,
                entry_high=65000,
                entry_price=65000,
                stop=64000,
                target=67000,
            )
            ledger.record_strategy(
                strategy_id="new",
                symbol=signal.symbol,
                source_chat_id=-100,
                source_sender_id=1818,
                source_message_id=2,
                raw_text="new",
                directions=("long",),
                signals=(signal,),
            )
            attempt = ledger.list_attempts("new")[0]
            ledger.transition_attempt(
                attempt.attempt_id,
                expected_states=("READY",),
                new_state="PARTIALLY_FILLED",
                ord_id="ENTRY-1",
                requested_size="10",
                filled_size="3",
            )
            gateway = FakeProtectionGateway()
            gateway.place_protection = lambda payload: {
                "code": "1",
                "msg": "rejected",
                "data": [],
            }

            result = ProtectionManager(ledger=ledger, gateway=gateway).ensure_coverage(
                attempt.attempt_id
            )

            self.assertEqual(ProtectionStatus.NEEDS_REVIEW, result.status)
            self.assertEqual([("BTC-USDT-SWAP", "ENTRY-1")], gateway.cancelled_entries)
            self.assertEqual("PROTECTION_FAILED", ledger.get_attempt(attempt.attempt_id).state)

    def test_later_partial_fill_expands_existing_protection_to_total_filled_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            signal = TradeSignal(
                symbol="BTC-USDT-SWAP",
                direction="long",
                entry_low=65000,
                entry_high=65000,
                entry_price=65000,
                stop=64000,
                target=67000,
            )
            ledger.record_strategy(
                strategy_id="new",
                symbol=signal.symbol,
                source_chat_id=-100,
                source_sender_id=1818,
                source_message_id=2,
                raw_text="new",
                directions=("long",),
                signals=(signal,),
            )
            attempt = ledger.list_attempts("new")[0]
            ledger.transition_attempt(
                attempt.attempt_id,
                expected_states=("READY",),
                new_state="PARTIALLY_FILLED",
                ord_id="ENTRY-1",
                requested_size="10",
                filled_size="3",
            )
            ledger.record_protection(
                attempt.attempt_id,
                algo_id="ALGO-1",
                covered_size="3",
                state="ACTIVE",
            )
            ledger.transition_attempt(
                attempt.attempt_id,
                expected_states=("PARTIALLY_FILLED",),
                new_state="PARTIALLY_FILLED",
                filled_size="5",
            )
            gateway = FakeProtectionGateway()

            result = ProtectionManager(ledger=ledger, gateway=gateway).ensure_coverage(
                attempt.attempt_id
            )

            self.assertEqual(ProtectionStatus.ACTIVE, result.status)
            self.assertEqual(
                [("BTC-USDT-SWAP", "ALGO-1", "5")], gateway.amended
            )
            self.assertEqual(
                "5", ledger.get_protection(attempt.attempt_id).covered_size
            )
            self.assertEqual([], gateway.placed)

    def test_lost_protection_response_recovers_existing_algo_without_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            signal = TradeSignal(
                symbol="BTC-USDT-SWAP",
                direction="long",
                entry_low=65000,
                entry_high=65000,
                entry_price=65000,
                stop=64000,
                target=67000,
            )
            ledger.record_strategy(
                strategy_id="new",
                symbol=signal.symbol,
                source_chat_id=-100,
                source_sender_id=1818,
                source_message_id=2,
                raw_text="new",
                directions=("long",),
                signals=(signal,),
            )
            attempt = ledger.list_attempts("new")[0]
            ledger.transition_attempt(
                attempt.attempt_id,
                expected_states=("READY",),
                new_state="PARTIALLY_FILLED",
                ord_id="ENTRY-1",
                requested_size="10",
                filled_size="3",
            )
            gateway = FakeProtectionGateway()
            gateway.active_algos = [{
                "instId": signal.symbol,
                "algoId": "ALGO-RECOVERED",
                "state": "live", "side": "sell", "tpTriggerPx": "67000", "slTriggerPx": "64000",
                "tpOrdPx": "-1", "slOrdPx": "-1", "tpTriggerPxType": "mark", "slTriggerPxType": "mark",
                "algoClOrdId": attempt.cl_ord_id + "P",
                "ordType": "oco",
                "posSide": "long",
                "sz": "3",
            }]
            gateway.place_protection = lambda payload: (_ for _ in ()).throw(
                TimeoutError("response lost")
            )

            result = ProtectionManager(ledger=ledger, gateway=gateway).ensure_coverage(
                attempt.attempt_id
            )

            self.assertEqual(ProtectionStatus.ACTIVE, result.status)
            self.assertEqual(
                "ALGO-RECOVERED", ledger.get_protection(attempt.attempt_id).algo_id
            )
            self.assertEqual([], gateway.cancelled_entries)

    def test_cancel_ack_without_terminal_state_keeps_protection_failure_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            signal = TradeSignal(
                symbol="BTC-USDT-SWAP",
                direction="long",
                entry_low=65000,
                entry_high=65000,
                entry_price=65000,
                stop=64000,
                target=67000,
            )
            ledger.record_strategy(
                strategy_id="new",
                symbol=signal.symbol,
                source_chat_id=-100,
                source_sender_id=1818,
                source_message_id=2,
                raw_text="new",
                directions=("long",),
                signals=(signal,),
            )
            attempt = ledger.list_attempts("new")[0]
            ledger.transition_attempt(
                attempt.attempt_id,
                expected_states=("READY",),
                new_state="PARTIALLY_FILLED",
                ord_id="ENTRY-1",
                requested_size="10",
                filled_size="3",
            )
            gateway = FakeProtectionGateway()
            gateway.place_protection = lambda payload: {
                "code": "1",
                "data": [],
            }
            gateway.order = lambda *args, **kwargs: {
                "code": "0",
                "data": [{
                    "instId": signal.symbol,
                    "ordId": "ENTRY-1",
                    "clOrdId": attempt.cl_ord_id,
                    "state": "live",
                    "accFillSz": "3",
                }],
            }

            result = ProtectionManager(ledger=ledger, gateway=gateway).ensure_coverage(
                attempt.attempt_id
            )

            self.assertEqual(ProtectionStatus.NEEDS_REVIEW, result.status)
            self.assertEqual("UNKNOWN", ledger.get_attempt(attempt.attempt_id).state)


if __name__ == "__main__":
    unittest.main()
