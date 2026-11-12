import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from src_v2.executor import ExecutionStatus, Executor
from src_v2.ledger import Ledger
from src_v2.parser import TradeSignal


class FakeGateway:
    def __init__(self):
        self.cancelled = []
        self.placed = []

    def positions(self, symbol):
        return []

    def pending_orders(self, symbol):
        return []

    def available_usdt(self):
        return Decimal("1000")

    def instrument(self, symbol):
        return {
            "tickSz": "0.1",
            "lotSz": "0.01",
            "minSz": "0.01",
            "ctVal": "0.01",
            "ctMult": "1",
        }

    def current_price(self, symbol):
        return Decimal("66000")

    def cancel_order(self, symbol, ord_id):
        self.cancelled.append((symbol, ord_id))
        return {"code": "0", "data": [{"ordId": ord_id, "sCode": "0"}]}

    def order(self, symbol, *, ord_id=None, cl_ord_id=None):
        state = "canceled" if str(ord_id).startswith("OLD") else "live"
        return {
            "code": "0",
            "data": [{
                "instId": symbol,
                "ordId": ord_id,
                "clOrdId": cl_ord_id,
                "state": state,
                "accFillSz": "0",
                "attachAlgoOrds": next((o["attachAlgoOrds"] for o in self.placed if o["clOrdId"] == cl_ord_id), []),
            }],
        }

    def ensure_leverage(self, symbol, direction, leverage):
        return {"code": "0", "data": [{"sCode": "0"}]}

    def place_order(self, order):
        self.placed.append(order)
        return {
            "code": "0",
            "data": [{
                "ordId": "NEW-1",
                "clOrdId": order["clOrdId"],
                "sCode": "0",
            }],
        }


class ExecutorBehaviorTests(unittest.TestCase):
    def test_invalid_new_strategy_never_cancels_an_existing_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            ledger.record_strategy(
                strategy_id="new",
                symbol="BTC-USDT-SWAP",
                source_chat_id=-100,
                source_sender_id=1818,
                source_message_id=2,
                raw_text="invalid",
                directions=("long",),
            )
            gateway = FakeGateway()
            gateway.pending_orders = lambda symbol: [
                {
                    "instId": symbol,
                    "ordId": "MANUAL-1",
                    "clOrdId": "CLI-MANUAL",
                    "tag": "CLI",
                    "ordType": "limit",
                    "state": "live",
                }
            ]
            invalid = TradeSignal(
                symbol="BTC-USDT-SWAP",
                direction="long",
                entry_low=65000,
                entry_high=65000,
                entry_price=65000,
                stop=66000,
                target=67000,
            )

            outcome = Executor(ledger=ledger, gateway=gateway).execute_strategy(
                "new", (invalid,)
            )

            self.assertEqual(ExecutionStatus.NEEDS_REVIEW, outcome.status)
            self.assertEqual([], gateway.cancelled)
            self.assertEqual([], gateway.placed)

    def test_valid_strategy_replaces_only_the_owned_old_entry_and_places_one_order(self):
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
                ord_id="OLD-OKX",
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
            gateway = FakeGateway()
            gateway.pending_orders = lambda symbol: [
                {
                    "instId": symbol,
                    "ordId": "OLD-OKX",
                    "clOrdId": old_attempt.cl_ord_id,
                    "tag": "BANGZHU",
                    "ordType": "limit",
                    "state": "live",
                },
                {
                    "instId": symbol,
                    "ordId": "MANUAL-1",
                    "clOrdId": "CLI-MANUAL",
                    "tag": "CLI",
                    "ordType": "limit",
                    "state": "live",
                },
            ]
            valid = TradeSignal(
                symbol="BTC-USDT-SWAP",
                direction="long",
                entry_low=65000,
                entry_high=65000,
                entry_price=65000,
                stop=64000,
                target=67000,
            )

            outcome = Executor(ledger=ledger, gateway=gateway).execute_strategy(
                "new", (valid,)
            )

            self.assertEqual(ExecutionStatus.SUBMITTED, outcome.status)
            self.assertEqual([("BTC-USDT-SWAP", "OLD-OKX")], gateway.cancelled)
            self.assertEqual(1, len(gateway.placed))
            self.assertEqual("BANGZHU", gateway.placed[0]["tag"])
            attempt = ledger.list_attempts("new")[0]
            snapshot = ledger.get_order_plan_snapshot(attempt.attempt_id)
            self.assertEqual("1000", snapshot.available_usdt)
            self.assertEqual(50, snapshot.leverage)
            self.assertEqual("0.00988", snapshot.margin_fraction)
            self.assertEqual("0.76", snapshot.size)

    def test_network_timeout_becomes_unknown_without_automatic_resubmission(self):
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
            gateway = FakeGateway()
            calls = []

            def timeout(order):
                calls.append(order)
                raise TimeoutError("response lost")

            gateway.place_order = timeout
            signal = TradeSignal(
                symbol="BTC-USDT-SWAP",
                direction="long",
                entry_low=65000,
                entry_high=65000,
                entry_price=65000,
                stop=64000,
                target=67000,
            )

            outcome = Executor(ledger=ledger, gateway=gateway).execute_strategy(
                "new", (signal,)
            )

            self.assertEqual(ExecutionStatus.UNKNOWN, outcome.status)
            self.assertEqual(1, len(calls))
            self.assertEqual("UNKNOWN", ledger.list_attempts("new")[0].state)

    def test_leverage_failure_preserves_the_owned_old_order(self):
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
                ord_id="OLD-OKX",
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
            gateway = FakeGateway()
            gateway.pending_orders = lambda symbol: [
                {
                    "instId": symbol,
                    "ordId": "OLD-OKX",
                    "clOrdId": old_attempt.cl_ord_id,
                    "tag": "BANGZHU",
                    "ordType": "limit",
                    "state": "live",
                }
            ]
            gateway.ensure_leverage = lambda *args: {
                "code": "1",
                "msg": "rejected",
                "data": [],
            }
            signal = TradeSignal(
                symbol="BTC-USDT-SWAP",
                direction="long",
                entry_low=65000,
                entry_high=65000,
                entry_price=65000,
                stop=64000,
                target=67000,
            )

            outcome = Executor(ledger=ledger, gateway=gateway).execute_strategy(
                "new", (signal,)
            )

            self.assertEqual(ExecutionStatus.NEEDS_REVIEW, outcome.status)
            self.assertEqual([], gateway.cancelled)

    def test_dual_direction_retry_skips_the_already_submitted_side(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            long_signal = TradeSignal(
                symbol="BTC-USDT-SWAP",
                direction="long",
                entry_low=65000,
                entry_high=65000,
                entry_price=65000,
                stop=64000,
                target=67000,
            )
            short_signal = TradeSignal(
                symbol="BTC-USDT-SWAP",
                direction="short",
                entry_low=67000,
                entry_high=67000,
                entry_price=67000,
                stop=68000,
                target=65000,
            )
            signals = (long_signal, short_signal)
            ledger.record_strategy(
                strategy_id="dual",
                symbol="BTC-USDT-SWAP",
                source_chat_id=-100,
                source_sender_id=1818,
                source_message_id=3,
                raw_text="dual",
                directions=("long", "short"),
                signals=signals,
            )
            gateway = FakeGateway()
            calls = []

            def fail_second(order):
                gateway.placed.append(order)
                calls.append(order)
                if len(calls) == 1:
                    return {
                        "code": "0",
                        "data": [{
                            "ordId": "LONG-1",
                            "clOrdId": order["clOrdId"],
                            "sCode": "0",
                        }],
                    }
                raise TimeoutError("short response lost")

            gateway.place_order = fail_second
            first = Executor(ledger=ledger, gateway=gateway).execute_strategy(
                "dual", signals
            )

            self.assertEqual(ExecutionStatus.UNKNOWN, first.status)
            self.assertEqual(1, first.placed)
            attempts = {item.direction: item for item in ledger.list_attempts("dual")}
            self.assertEqual("LIVE", attempts["long"].state)
            self.assertEqual("UNKNOWN", attempts["short"].state)
            old_short_client_id = attempts["short"].cl_ord_id

            ledger.transition_attempt(
                attempts["short"].attempt_id,
                expected_states=("UNKNOWN",),
                new_state="MISSING_CONFIRMED",
            )
            retried = ledger.prepare_confirmed_missing_retry(
                attempts["short"].attempt_id
            )
            self.assertNotEqual(old_short_client_id, retried.cl_ord_id)
            retry_gateway = FakeGateway()
            retried_orders = []

            def place_retry(order):
                retry_gateway.placed.append(order)
                retried_orders.append(order)
                return {
                    "code": "0",
                    "data": [{
                        "ordId": "SHORT-2",
                        "clOrdId": order["clOrdId"],
                        "sCode": "0",
                    }],
                }

            retry_gateway.place_order = place_retry

            second = Executor(
                ledger=ledger, gateway=retry_gateway
            ).execute_strategy("dual", signals)

            self.assertEqual(ExecutionStatus.SUBMITTED, second.status)
            self.assertEqual(1, second.placed)
            self.assertEqual(1, len(retried_orders))
            self.assertEqual("short", retried_orders[0]["posSide"])
            final_attempts = {
                item.direction: item for item in ledger.list_attempts("dual")
            }
            self.assertEqual("LONG-1", final_attempts["long"].ord_id)
            self.assertEqual("SHORT-2", final_attempts["short"].ord_id)
            self.assertEqual("LIVE", final_attempts["short"].state)

    def test_position_query_failure_fails_closed_without_touching_orders(self):
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
            gateway = FakeGateway()
            gateway.positions = lambda symbol: (_ for _ in ()).throw(
                TimeoutError("positions unavailable")
            )

            outcome = Executor(ledger=ledger, gateway=gateway).execute_strategy(
                "new", (signal,)
            )

            self.assertEqual(ExecutionStatus.RETRYABLE, outcome.status)
            self.assertEqual([], gateway.cancelled)
            self.assertEqual([], gateway.placed)

    def test_authorized_dual_strategy_keeps_opposite_side_after_one_side_fills(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            long_signal = TradeSignal(
                symbol="BTC-USDT-SWAP",
                direction="long",
                entry_low=65000,
                entry_high=65000,
                entry_price=65000,
                stop=64000,
                target=67000,
            )
            short_signal = TradeSignal(
                symbol="BTC-USDT-SWAP",
                direction="short",
                entry_low=67000,
                entry_high=67000,
                entry_price=67000,
                stop=68000,
                target=65000,
            )
            signals = (long_signal, short_signal)
            ledger.record_strategy(
                strategy_id="dual",
                symbol="BTC-USDT-SWAP",
                source_chat_id=-100,
                source_sender_id=1818,
                source_message_id=3,
                raw_text="dual",
                directions=("long", "short"),
                signals=signals,
            )
            attempts = {
                item.direction: item for item in ledger.list_attempts("dual")
            }
            ledger.transition_attempt(
                attempts["long"].attempt_id,
                expected_states=("READY",),
                new_state="FILLED",
                ord_id="LONG-1",
                requested_size="2",
                filled_size="2",
            )
            ledger.record_protection(
                attempts["long"].attempt_id,
                algo_id="ALGO-LONG",
                covered_size="2",
                state="ACTIVE",
            )
            gateway = FakeGateway()
            gateway.positions = lambda symbol: [
                {"instId": symbol, "posSide": "long", "pos": "2"}
            ]

            outcome = Executor(ledger=ledger, gateway=gateway).execute_strategy(
                "dual", signals
            )

            self.assertEqual(ExecutionStatus.SUBMITTED, outcome.status)
            self.assertEqual(1, outcome.placed)
            self.assertEqual("short", gateway.placed[0]["posSide"])

    def test_partial_pending_order_blocks_replacement_even_if_position_api_lags(self):
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
            signal = TradeSignal(
                symbol="BTC-USDT-SWAP",
                direction="long",
                entry_low=65000,
                entry_high=65000,
                entry_price=65000,
                stop=64000,
                target=67000,
            )
            gateway = FakeGateway()
            gateway.pending_orders = lambda symbol: [
                {
                    "instId": symbol,
                    "ordId": "PARTIAL-1",
                    "clOrdId": "CLI-MANUAL",
                    "tag": "CLI",
                    "ordType": "limit",
                    "state": "partially_filled",
                }
            ]

            outcome = Executor(ledger=ledger, gateway=gateway).execute_strategy(
                "new", (signal,)
            )

            self.assertEqual(ExecutionStatus.NEEDS_REVIEW, outcome.status)
            self.assertEqual([], gateway.cancelled)
            self.assertEqual([], gateway.placed)

    def test_cancel_acknowledgement_without_terminal_confirmation_blocks_new_order(self):
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
            old = ledger.list_attempts("old")[0]
            ledger.transition_attempt(
                old.attempt_id,
                expected_states=("READY",),
                new_state="LIVE",
                ord_id="OLD-1",
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
            signal = TradeSignal(
                symbol="BTC-USDT-SWAP",
                direction="long",
                entry_low=65000,
                entry_high=65000,
                entry_price=65000,
                stop=64000,
                target=67000,
            )
            gateway = FakeGateway()
            gateway.pending_orders = lambda symbol: [{
                "instId": symbol,
                "ordId": "OLD-1",
                "clOrdId": old.cl_ord_id,
                "tag": "BANGZHU",
                "ordType": "limit",
                "state": "live",
            }]
            gateway.order = lambda *args, **kwargs: {
                "code": "0",
                "data": [{
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "OLD-1",
                    "clOrdId": old.cl_ord_id,
                    "state": "live",
                    "accFillSz": "0",
                }],
            }

            outcome = Executor(ledger=ledger, gateway=gateway).execute_strategy(
                "new", (signal,)
            )

            self.assertEqual(ExecutionStatus.UNKNOWN, outcome.status)
            self.assertEqual([], gateway.placed)
            self.assertEqual("CANCEL_UNKNOWN", ledger.get_attempt(old.attempt_id).state)

    def test_mismatched_client_order_id_in_submit_response_is_never_trusted(self):
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
            gateway = FakeGateway()
            gateway.place_order = lambda order: {
                "code": "0",
                "data": [{
                    "ordId": "SOMEONE-ELSE",
                    "clOrdId": "OTHER-CLIENT-ID",
                    "sCode": "0",
                }],
            }

            outcome = Executor(ledger=ledger, gateway=gateway).execute_strategy(
                "new", (signal,)
            )

            self.assertEqual(ExecutionStatus.UNKNOWN, outcome.status)
            attempt = ledger.list_attempts("new")[0]
            self.assertEqual("UNKNOWN", attempt.state)
            self.assertIsNone(attempt.ord_id)

    def test_orphaned_system_tagged_order_blocks_new_order_instead_of_being_ignored(self):
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
            gateway = FakeGateway()
            gateway.pending_orders = lambda symbol: [{
                "instId": symbol,
                "ordId": "ORPHAN-1",
                "clOrdId": "BZorphanedorderidentifier",
                "tag": "BANGZHU",
                "ordType": "limit",
                "state": "live",
            }]

            outcome = Executor(ledger=ledger, gateway=gateway).execute_strategy(
                "new", (signal,)
            )

            self.assertEqual(ExecutionStatus.NEEDS_REVIEW, outcome.status)
            self.assertEqual([], gateway.cancelled)
            self.assertEqual([], gateway.placed)

    def test_immediate_fill_is_protected_before_execute_returns(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            signal = TradeSignal(
                symbol="BTC-USDT-SWAP",
                direction="long",
                entry_low=65000,
                entry_high=65000,
                entry_price=65000,
                stop=63000,
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
            gateway = FakeGateway()
            gateway.current_price = lambda symbol: Decimal("64500")
            active_algos = []

            def filled_order(symbol, *, ord_id=None, cl_ord_id=None):
                terms = gateway.placed[-1]["attachAlgoOrds"][0]
                active_algos[:] = [{**terms, "instId": symbol, "algoId": "ALGO-1",
                    "algoClOrdId": terms["attachAlgoClOrdId"], "ordType": "oco", "state": "live",
                    "posSide": "long", "side": "sell", "sz": "2"}]
                return {
                    "code": "0",
                    "data": [{
                        "instId": symbol,
                        "ordId": ord_id,
                        "clOrdId": cl_ord_id,
                        "state": "filled",
                        "accFillSz": "2",
                    }],
                }

            def place_protection(payload):
                self.fail("Native attachment must not submit a standalone duplicate")
                active_algos.append({
                    "instId": payload["instId"],
                    "algoId": "ALGO-1",
                    "algoClOrdId": payload["algoClOrdId"],
                    "ordType": "oco",
                    "posSide": payload["posSide"],
                    "sz": payload["sz"],
                })
                return {
                    "code": "0",
                    "data": [{"algoId": "ALGO-1", "sCode": "0"}],
                }

            gateway.order = filled_order
            gateway.place_protection = place_protection
            gateway.algo_orders_response = lambda symbol: {
                "code": "0",
                "data": active_algos,
            }

            outcome = Executor(ledger=ledger, gateway=gateway).execute_strategy(
                "new", (signal,)
            )

            self.assertEqual(ExecutionStatus.SUBMITTED, outcome.status)
            attempt = ledger.list_attempts("new")[0]
            self.assertEqual("FILLED", attempt.state)
            self.assertEqual("2", ledger.get_protection(attempt.attempt_id).covered_size)


if __name__ == "__main__":
    unittest.main()
