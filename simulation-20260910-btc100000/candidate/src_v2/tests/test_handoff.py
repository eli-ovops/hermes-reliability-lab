import tempfile
import unittest
from pathlib import Path

from src_v2.handoff import (
    HandoffError,
    Ownership,
    adopt_approved_legacy_order,
    build_handoff_report,
    cancel_approved_legacy_orders,
)
from src_v2.ledger import Ledger
from src_v2.parser import TradeSignal


class ReadOnlyGateway:
    def __init__(self):
        self.cancelled = []
        self.v2_client_id = ""

    def pending_orders(self, symbol):
        return [
            {
                "instId": symbol,
                "ordId": "V2-ORDER",
                "clOrdId": self.v2_client_id,
                "tag": "BANGZHU",
                "state": "live",
                "accFillSz": "0",
            },
            {
                "instId": symbol,
                "ordId": "LEGACY-ORDER",
                "clOrdId": "BZlegacy123",
                "tag": "BANGZHU",
                "state": "live",
                "accFillSz": "0",
            },
            {
                "instId": symbol,
                "ordId": "MANUAL-ORDER",
                "clOrdId": "",
                "tag": "",
                "state": "live",
                "accFillSz": "0",
            },
        ]

    def positions(self, symbol):
        return [{"instId": symbol, "posSide": "long", "pos": "1"}]

    def cancel_order(self, symbol, order_id):
        self.cancelled.append(order_id)
        return {"code": "0", "data": [{"ordId": order_id, "sCode": "0"}]}

    def order(self, symbol, *, ord_id=None, cl_ord_id=None):
        return {
            "code": "0",
            "data": [{
                "instId": symbol,
                "ordId": ord_id,
                "clOrdId": cl_ord_id,
                "state": "canceled",
                "accFillSz": "0",
            }],
        }


class AdoptionGateway:
    def __init__(self):
        self.order_data = {
            "instId": "BTC-USDT-SWAP",
            "ordId": "LEGACY-ORDER",
            "clOrdId": "BZlegacy123S",
            "tag": "BANGZHU",
            "side": "sell",
            "posSide": "short",
            "ordType": "limit",
            "px": "82750",
            "sz": "0.69",
            "state": "live",
            "accFillSz": "0",
            "attachAlgoOrds": [{
                "attachAlgoClOrdId": "BZlegacy123SP",
                "tpTriggerPx": "80500",
                "tpOrdPx": "-1",
                "slTriggerPx": "83500",
                "slOrdPx": "-1",
            }],
        }

    def pending_orders(self, symbol):
        return [dict(self.order_data)] if symbol == self.order_data["instId"] else []

    def positions(self, symbol):
        return []

    def order(self, symbol, *, ord_id=None, cl_ord_id=None):
        return {"code": "0", "data": [dict(self.order_data)]}


class LegacyHandoffTests(unittest.TestCase):
    def test_exact_protected_zero_fill_legacy_order_is_adopted_without_exchange_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            gateway = AdoptionGateway()
            signal = TradeSignal(
                symbol="BTC-USDT-SWAP",
                direction="short",
                entry_low=82500,
                entry_high=83000,
                entry_price=82750,
                stop=83500,
                target=80500,
            )

            adopted = adopt_approved_legacy_order(
                ledger=ledger,
                gateway=gateway,
                approved_order_id="LEGACY-ORDER",
                strategy_id="adopted-tg-67364",
                source_chat_id=-1002069530742,
                source_sender_id=5356537814,
                source_message_id=67364,
                raw_text="BTC 完整空单策略",
                signal=signal,
                expected_size="0.69",
            )

            self.assertEqual("LIVE", adopted.state)
            self.assertEqual("LEGACY-ORDER", adopted.ord_id)
            self.assertEqual("BZlegacy123S", adopted.cl_ord_id)
            self.assertEqual("0.69", adopted.requested_size)
            self.assertEqual(signal, ledger.get_attempt_signal(adopted.attempt_id))
            report = build_handoff_report(
                ledger=ledger,
                gateway=gateway,
                symbols=("BTC-USDT-SWAP",),
            )
            self.assertEqual(Ownership.V2_OWNED, report.orders[0].ownership)

    def test_legacy_adoption_rejects_a_mismatched_size_without_writing_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            gateway = AdoptionGateway()
            signal = TradeSignal(
                symbol="BTC-USDT-SWAP",
                direction="short",
                entry_low=82500,
                entry_high=83000,
                entry_price=82750,
                stop=83500,
                target=80500,
            )

            with self.assertRaisesRegex(HandoffError, "数量"):
                adopt_approved_legacy_order(
                    ledger=ledger,
                    gateway=gateway,
                    approved_order_id="LEGACY-ORDER",
                    strategy_id="adopted-tg-67364",
                    source_chat_id=-1002069530742,
                    source_sender_id=5356537814,
                    source_message_id=67364,
                    raw_text="BTC 完整空单策略",
                    signal=signal,
                    expected_size="0.70",
                )

            self.assertIsNone(ledger.get_strategy("adopted-tg-67364"))

    def test_report_distinguishes_v2_legacy_and_foreign_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            ledger.record_strategy(
                strategy_id="v2-existing",
                symbol="ETH-USDT-SWAP",
                source_chat_id=-100,
                source_sender_id=1,
                source_message_id=1,
                raw_text="existing",
                directions=("long",),
            )
            attempt = ledger.list_attempts("v2-existing")[0]
            ledger.transition_attempt(
                attempt.attempt_id,
                expected_states=("READY",),
                new_state="LIVE",
                ord_id="V2-ORDER",
                requested_size="1",
            )
            gateway = ReadOnlyGateway()
            gateway.v2_client_id = attempt.cl_ord_id

            report = build_handoff_report(
                ledger=ledger,
                gateway=gateway,
                symbols=("ETH-USDT-SWAP",),
            )

            self.assertEqual(
                [
                    Ownership.V2_OWNED,
                    Ownership.LEGACY_UNCLAIMED,
                    Ownership.FOREIGN,
                ],
                [item.ownership for item in report.orders],
            )
            self.assertEqual(1, len(report.positions))
            self.assertTrue(report.requires_user_decision)

    def test_only_an_exact_approved_unfilled_legacy_order_can_be_canceled(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            gateway = ReadOnlyGateway()

            canceled = cancel_approved_legacy_orders(
                ledger=ledger,
                gateway=gateway,
                symbols=("ETH-USDT-SWAP",),
                approved_order_ids=("LEGACY-ORDER",),
            )

            self.assertEqual(("LEGACY-ORDER",), canceled)
            self.assertEqual(["LEGACY-ORDER"], gateway.cancelled)

    def test_manual_order_cannot_be_approved_as_legacy_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            gateway = ReadOnlyGateway()

            with self.assertRaises(HandoffError):
                cancel_approved_legacy_orders(
                    ledger=ledger,
                    gateway=gateway,
                    symbols=("ETH-USDT-SWAP",),
                    approved_order_ids=("MANUAL-ORDER",),
                )

            self.assertEqual([], gateway.cancelled)

    def test_malformed_cancel_response_becomes_a_handoff_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            gateway = ReadOnlyGateway()
            gateway.cancel_order = lambda symbol, order_id: None

            with self.assertRaises(HandoffError):
                cancel_approved_legacy_orders(
                    ledger=ledger,
                    gateway=gateway,
                    symbols=("ETH-USDT-SWAP",),
                    approved_order_ids=("LEGACY-ORDER",),
                )


if __name__ == "__main__":
    unittest.main()
