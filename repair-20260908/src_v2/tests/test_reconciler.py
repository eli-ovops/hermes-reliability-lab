import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src_v2.ledger import Ledger
from src_v2.parser import TradeSignal
from src_v2.reconciler import ReconcileStatus, Reconciler


class FakeReconcileGateway:
    def __init__(self, order_response):
        self.order_response = order_response
        self.active_algos = []

    def order(self, symbol, *, ord_id=None, cl_ord_id=None):
        return self.order_response

    def pending_orders_response(self, symbol):
        return {"code": "0", "data": []}

    def order_history_response(self, symbol):
        return {"code": "0", "data": []}

    def order_archive_response(self, symbol):
        return {"code": "0", "data": []}

    def fills_response(self, symbol, *, ord_id=None):
        return {"code": "0", "data": []}

    def positions_response(self, symbol):
        return {"code": "0", "data": []}

    def algo_orders_response(self, symbol):
        return {"code": "0", "data": list(self.active_algos)}

    def place_protection(self, payload):
        self.active_algos.append(
            {
                "instId": payload["instId"],
                "algoId": "ALGO-1",
                "algoClOrdId": payload["algoClOrdId"],
                "ordType": payload["ordType"],
                "posSide": payload["posSide"],
                "sz": payload["sz"],
            }
        )
        return {"code": "0", "data": [{"algoId": "ALGO-1", "sCode": "0"}]}

    def amend_protection(self, symbol, algo_id, new_size):
        return {"code": "0", "data": [{"algoId": algo_id, "sCode": "0"}]}

    def cancel_entry(self, symbol, ord_id):
        return {"code": "0", "data": [{"ordId": ord_id, "sCode": "0"}]}


class FlakyOrderGateway(FakeReconcileGateway):
    def __init__(self, outcomes):
        super().__init__({"code": "0", "data": []})
        self.outcomes = list(outcomes)
        self.order_calls = 0

    def order(self, symbol, *, ord_id=None, cl_ord_id=None):
        self.order_calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FailedProtectionHistoryGateway(FakeReconcileGateway):
    def __init__(self, order_response, history_item):
        super().__init__(order_response)
        self.history_item = history_item

    def algo_history_response(self, symbol, *, algo_id):
        return {"code": "0", "data": [dict(self.history_item)]}


class ReconcilerBehaviorTests(unittest.TestCase):
    @staticmethod
    def _after_grace():
        return datetime.now(timezone.utc) + timedelta(minutes=5)
    def test_lost_submit_response_recovers_the_existing_live_order(self):
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
            ledger.transition_attempt(
                attempt.attempt_id,
                expected_states=("READY",),
                new_state="UNKNOWN",
                requested_size="2",
            )
            gateway = FakeReconcileGateway(
                {
                    "code": "0",
                    "data": [
                        {
                            "instId": "BTC-USDT-SWAP",
                            "ordId": "RECOVERED-1",
                            "clOrdId": attempt.cl_ord_id,
                            "state": "live",
                            "accFillSz": "0",
                        }
                    ],
                }
            )

            result = Reconciler(
                ledger=ledger,
                gateway=gateway,
                now_provider=self._after_grace,
            ).reconcile(
                attempt.attempt_id
            )

            self.assertEqual(ReconcileStatus.LIVE, result.status)
            recovered = ledger.get_attempt(attempt.attempt_id)
            self.assertEqual("LIVE", recovered.state)
            self.assertEqual("RECOVERED-1", recovered.ord_id)

    def test_transient_order_detail_exception_retries_once_without_review(self):
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
            ledger.transition_attempt(
                attempt.attempt_id,
                expected_states=("READY",),
                new_state="LIVE",
                ord_id="ENTRY-1",
                requested_size="1",
            )
            gateway = FlakyOrderGateway(
                [
                    TimeoutError("temporary network failure"),
                    {
                        "code": "0",
                        "data": [
                            {
                                "instId": "BTC-USDT-SWAP",
                                "ordId": "ENTRY-1",
                                "clOrdId": attempt.cl_ord_id,
                                "state": "live",
                                "accFillSz": "0",
                            }
                        ],
                    },
                ]
            )

            result = Reconciler(ledger=ledger, gateway=gateway).reconcile(
                attempt.attempt_id
            )

            self.assertEqual(ReconcileStatus.LIVE, result.status)
            self.assertEqual(2, gateway.order_calls)

    def test_persistent_order_detail_exception_reports_only_exception_type(self):
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
            ledger.transition_attempt(
                attempt.attempt_id,
                expected_states=("READY",),
                new_state="LIVE",
                ord_id="ENTRY-1",
                requested_size="1",
            )
            gateway = FlakyOrderGateway(
                [
                    TimeoutError("secret-bearing upstream detail"),
                    TimeoutError("secret-bearing upstream detail"),
                ]
            )

            result = Reconciler(ledger=ledger, gateway=gateway).reconcile(
                attempt.attempt_id
            )

            self.assertEqual(ReconcileStatus.NEEDS_REVIEW, result.status)
            self.assertEqual("订单详情查询异常 (TimeoutError)", result.reason)
            self.assertNotIn("secret-bearing", result.reason)
            self.assertEqual(2, gateway.order_calls)

    def test_order_missing_from_detail_is_recovered_from_pending_orders(self):
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
            ledger.transition_attempt(
                attempt.attempt_id,
                expected_states=("READY",),
                new_state="UNKNOWN",
                requested_size="2",
            )
            gateway = FakeReconcileGateway(
                {"code": "51603", "msg": "Order does not exist", "data": []}
            )
            gateway.pending_orders_response = lambda symbol: {
                "code": "0",
                "data": [
                    {
                        "instId": symbol,
                        "ordId": "PENDING-1",
                        "clOrdId": attempt.cl_ord_id,
                        "state": "live",
                        "accFillSz": "0",
                    }
                ],
            }

            result = Reconciler(
                ledger=ledger,
                gateway=gateway,
                now_provider=self._after_grace,
            ).reconcile(
                attempt.attempt_id
            )

            self.assertEqual(ReconcileStatus.LIVE, result.status)
            self.assertEqual("PENDING-1", ledger.get_attempt(attempt.attempt_id).ord_id)

    def test_retry_is_allowed_only_after_every_read_path_confirms_absence(self):
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
            ledger.transition_attempt(
                attempt.attempt_id,
                expected_states=("READY",),
                new_state="UNKNOWN",
                requested_size="2",
            )
            gateway = FakeReconcileGateway(
                {"code": "51603", "msg": "Order does not exist", "data": []}
            )

            result = Reconciler(
                ledger=ledger,
                gateway=gateway,
                now_provider=self._after_grace,
            ).reconcile(attempt.attempt_id)

            self.assertEqual(ReconcileStatus.MISSING_CONFIRMED, result.status)
            self.assertEqual("MISSING_CONFIRMED", ledger.get_attempt(attempt.attempt_id).state)

            repeated = Reconciler(ledger=ledger, gateway=gateway).reconcile(
                attempt.attempt_id
            )
            self.assertEqual(ReconcileStatus.MISSING_CONFIRMED, repeated.status)

    def test_all_empty_reads_inside_propagation_window_do_not_allow_retry(self):
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
            ledger.transition_attempt(
                attempt.attempt_id,
                expected_states=("READY",),
                new_state="UNKNOWN",
                requested_size="2",
            )
            gateway = FakeReconcileGateway(
                {"code": "51603", "msg": "Order does not exist", "data": []}
            )

            result = Reconciler(ledger=ledger, gateway=gateway).reconcile(
                attempt.attempt_id
            )

            self.assertEqual(ReconcileStatus.NEEDS_REVIEW, result.status)
            self.assertEqual("UNKNOWN", ledger.get_attempt(attempt.attempt_id).state)

    def test_filled_order_is_final_only_after_market_protection_is_created(self):
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
                new_state="SUBMITTED",
                ord_id="ENTRY-1",
                requested_size="5",
            )
            gateway = FakeReconcileGateway(
                {
                    "code": "0",
                    "data": [{
                        "instId": signal.symbol,
                        "ordId": "ENTRY-1",
                        "clOrdId": attempt.cl_ord_id,
                        "state": "filled",
                        "accFillSz": "5",
                    }],
                }
            )

            result = Reconciler(ledger=ledger, gateway=gateway).reconcile(
                attempt.attempt_id
            )

            self.assertEqual(ReconcileStatus.FILLED, result.status)
            self.assertEqual("FILLED", ledger.get_attempt(attempt.attempt_id).state)
            self.assertEqual(
                "5", ledger.get_protection(attempt.attempt_id).covered_size
            )

    def test_old_filled_order_with_confirmed_zero_position_closes_without_new_protection(self):
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
                strategy_id="old-filled",
                symbol=signal.symbol,
                source_chat_id=-100,
                source_sender_id=1818,
                source_message_id=20,
                raw_text="old filled",
                directions=("long",),
                signals=(signal,),
            )
            attempt = ledger.list_attempts("old-filled")[0]
            ledger.transition_attempt(
                attempt.attempt_id,
                expected_states=("READY",),
                new_state="SUBMITTED",
                ord_id="ENTRY-OLD",
                requested_size="5",
            )
            filled_at = datetime(2026, 8, 29, 2, 0, tzinfo=timezone.utc)
            gateway = FakeReconcileGateway(
                {
                    "code": "0",
                    "data": [{
                        "instId": signal.symbol,
                        "ordId": "ENTRY-OLD",
                        "clOrdId": attempt.cl_ord_id,
                        "state": "filled",
                        "accFillSz": "5",
                        "fillTime": str(int(filled_at.timestamp() * 1000)),
                    }],
                }
            )

            result = Reconciler(
                ledger=ledger,
                gateway=gateway,
                now_provider=lambda: datetime(2026, 8, 29, 3, 0, tzinfo=timezone.utc),
            ).reconcile(attempt.attempt_id)

            self.assertEqual(ReconcileStatus.CLOSED, result.status)
            self.assertEqual("CLOSED", ledger.get_attempt(attempt.attempt_id).state)
            self.assertIsNone(ledger.get_protection(attempt.attempt_id))
            self.assertEqual([], gateway.active_algos)

    def test_stale_protection_ledger_record_never_counts_as_verified_coverage(self):
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
                new_state="FILLED",
                ord_id="ENTRY-1",
                requested_size="5",
                filled_size="5",
            )
            ledger.record_protection(
                attempt.attempt_id,
                algo_id="ALGO-OLD",
                covered_size="5",
                state="ACTIVE",
            )
            gateway = FakeReconcileGateway(
                {
                    "code": "0",
                    "data": [{
                        "instId": signal.symbol,
                        "ordId": "ENTRY-1",
                        "clOrdId": attempt.cl_ord_id,
                        "state": "filled",
                        "accFillSz": "5",
                    }],
                }
            )

            result = Reconciler(ledger=ledger, gateway=gateway).reconcile(
                attempt.attempt_id
            )

            self.assertEqual(ReconcileStatus.NEEDS_REVIEW, result.status)
            self.assertIn("保护单", result.reason)

    def test_failed_protection_history_with_zero_position_closes_flat_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            signal = TradeSignal(
                symbol="BTC-USDT-SWAP",
                direction="short",
                entry_low=79750,
                entry_high=79750,
                entry_price=79750,
                stop=81000,
                target=77500,
            )
            ledger.record_strategy(
                strategy_id="flat-failed-protection",
                symbol=signal.symbol,
                source_chat_id=-100,
                source_sender_id=1818,
                source_message_id=21,
                raw_text="flat failed protection",
                directions=("short",),
                signals=(signal,),
            )
            attempt = ledger.list_attempts("flat-failed-protection")[0]
            ledger.transition_attempt(
                attempt.attempt_id,
                expected_states=("READY",),
                new_state="FILLED",
                ord_id="ENTRY-FLAT",
                requested_size="0.43",
                filled_size="0.43",
            )
            ledger.record_protection(
                attempt.attempt_id,
                algo_id="ALGO-FAILED",
                covered_size="0.43",
                state="ACTIVE",
            )
            gateway = FailedProtectionHistoryGateway(
                {
                    "code": "0",
                    "data": [{
                        "instId": signal.symbol,
                        "ordId": "ENTRY-FLAT",
                        "clOrdId": attempt.cl_ord_id,
                        "state": "filled",
                        "accFillSz": "0.43",
                    }],
                },
                {
                    "instId": signal.symbol,
                    "algoId": "ALGO-FAILED",
                    "algoClOrdId": attempt.cl_ord_id + "P",
                    "ordType": "oco",
                    "posSide": "short",
                    "sz": "0.43",
                    "state": "order_failed",
                },
            )

            result = Reconciler(ledger=ledger, gateway=gateway).reconcile(
                attempt.attempt_id
            )

            self.assertEqual(ReconcileStatus.CLOSED, result.status)
            self.assertEqual("CLOSED", ledger.get_attempt(attempt.attempt_id).state)
            self.assertEqual(
                "CANCELED", ledger.get_protection(attempt.attempt_id).state
            )

    def test_canceled_entry_with_partial_fill_is_protected_and_requires_review(self):
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
                new_state="SUBMITTED",
                ord_id="ENTRY-1",
                requested_size="5",
            )
            gateway = FakeReconcileGateway(
                {
                    "code": "0",
                    "data": [{
                        "instId": signal.symbol,
                        "ordId": "ENTRY-1",
                        "clOrdId": attempt.cl_ord_id,
                        "state": "canceled",
                        "accFillSz": "2",
                    }],
                }
            )

            result = Reconciler(ledger=ledger, gateway=gateway).reconcile(
                attempt.attempt_id
            )

            self.assertEqual(ReconcileStatus.NEEDS_REVIEW, result.status)
            self.assertEqual(
                "CANCELED_PARTIAL", ledger.get_attempt(attempt.attempt_id).state
            )
            self.assertEqual(
                "2", ledger.get_protection(attempt.attempt_id).covered_size
            )

    def test_protection_failed_attempt_is_reconciled_and_reprotected(self):
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
                new_state="PROTECTION_FAILED",
                ord_id="ENTRY-1",
                requested_size="5",
                filled_size="2",
                error="earlier protection failure",
            )
            gateway = FakeReconcileGateway(
                {
                    "code": "0",
                    "data": [{
                        "instId": signal.symbol,
                        "ordId": "ENTRY-1",
                        "clOrdId": attempt.cl_ord_id,
                        "state": "canceled",
                        "accFillSz": "2",
                    }],
                }
            )

            result = Reconciler(ledger=ledger, gateway=gateway).reconcile(
                attempt.attempt_id
            )

            self.assertEqual(ReconcileStatus.NEEDS_REVIEW, result.status)
            self.assertEqual(
                "CANCELED_PARTIAL", ledger.get_attempt(attempt.attempt_id).state
            )
            self.assertEqual(
                "ACTIVE", ledger.get_protection(attempt.attempt_id).state
            )


if __name__ == "__main__":
    unittest.main()
