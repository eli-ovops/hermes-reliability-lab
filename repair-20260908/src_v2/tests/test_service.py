import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from src_v2.executor import ExecutionStatus
from src_v2.ledger import Ledger
from src_v2.parser import TradeSignal
from src_v2.reconciler import ReconcileResult, ReconcileStatus
from src_v2.service import TradingService


class ServiceGateway:
    def __init__(self):
        self.placed = []
        self.placed_ids = {}

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

    def ensure_leverage(self, symbol, direction, leverage):
        return {"code": "0", "data": [{"sCode": "0"}]}

    def place_order(self, order):
        self.placed.append(order)
        self.placed_ids["ORDER-1"] = order["clOrdId"]
        return {
            "code": "0",
            "data": [{
                "ordId": "ORDER-1",
                "clOrdId": order["clOrdId"],
                "sCode": "0",
            }],
        }

    def order(self, symbol, *, ord_id=None, cl_ord_id=None):
        if ord_id in self.placed_ids:
            return {
                "code": "0",
                "data": [{
                    "instId": symbol,
                    "ordId": ord_id,
                    "clOrdId": self.placed_ids[ord_id],
                    "state": "live",
                    "accFillSz": "0",
                }],
            }
        return {"code": "51603", "msg": "Order does not exist", "data": []}

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
        return {"code": "0", "data": []}


class LaterFillGateway(ServiceGateway):
    def __init__(self):
        super().__init__()
        self.later_filled = False
        self.protections = []
        self.protection_triggered = False

    def order(self, symbol, *, ord_id=None, cl_ord_id=None):
        response = super().order(symbol, ord_id=ord_id, cl_ord_id=cl_ord_id)
        if response.get("code") == "0" and self.later_filled:
            response["data"][0]["state"] = "filled"
            response["data"][0]["accFillSz"] = "1"
        return response

    def place_protection(self, payload):
        self.protections.append({**payload, "algoId": "ALGO-1"})
        return {"code": "0", "data": [{"algoId": "ALGO-1", "sCode": "0"}]}

    def algo_orders_response(self, symbol):
        if self.protection_triggered:
            return {"code": "0", "data": []}
        return {
            "code": "0",
            "data": [
                {
                    **item,
                    "instId": symbol,
                    "ordType": "oco",
                }
                for item in self.protections
            ],
        }

    def algo_history_response(self, symbol, *, algo_id):
        if not self.protection_triggered:
            return {"code": "0", "data": []}
        item = self.protections[0]
        return {
            "code": "0",
            "data": [{**item, "instId": symbol, "state": "effective"}],
        }


class TransientOrderGateway(ServiceGateway):
    def __init__(self):
        super().__init__()
        self.fail_order_reads = False

    def order(self, symbol, *, ord_id=None, cl_ord_id=None):
        if self.fail_order_reads:
            raise TimeoutError("temporary order-detail timeout")
        return super().order(symbol, ord_id=ord_id, cl_ord_id=cl_ord_id)


class TradingServiceBehaviorTests(unittest.TestCase):
    @staticmethod
    def _after_grace():
        return datetime.now(timezone.utc) + timedelta(minutes=5)
    def _ledger_with_signal(self, directory):
        ledger = Ledger(Path(directory) / "ledger.sqlite3")
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
        return ledger

    def test_service_loads_authoritative_signal_from_ledger_and_queues_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger_with_signal(tmp)
            gateway = ServiceGateway()
            service = TradingService(
                ledger=ledger,
                gateway=gateway,
                lock_path=Path(tmp) / "executor.lock",
                now_provider=self._after_grace,
            )

            outcome = service.execute_strategy("new")

            self.assertEqual(ExecutionStatus.SUBMITTED, outcome.status)
            self.assertEqual(1, len(gateway.placed))
            self.assertEqual(1, len(ledger.list_pending_notifications()))

    def test_paused_automation_blocks_direct_service_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger_with_signal(tmp)
            ledger.pause_automation("微信通知连续失败")
            gateway = ServiceGateway()
            service = TradingService(
                ledger=ledger,
                gateway=gateway,
                lock_path=Path(tmp) / "executor.lock",
            )

            outcome = service.execute_strategy("new")

            self.assertEqual(ExecutionStatus.NEEDS_REVIEW, outcome.status)
            self.assertIn("自动化已暂停", outcome.reason)
            self.assertEqual([], gateway.placed)

    def test_live_service_rejects_a_strategy_without_authenticated_ingest(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger_with_signal(tmp)
            gateway = ServiceGateway()
            service = TradingService(
                ledger=ledger,
                gateway=gateway,
                lock_path=Path(tmp) / "executor.lock",
                require_source_attestation=True,
            )

            outcome = service.execute_strategy("new")

            self.assertEqual(ExecutionStatus.NEEDS_REVIEW, outcome.status)
            self.assertIn("来源审计", outcome.reason)
            self.assertEqual([], gateway.placed)

    def test_background_reconciliation_protects_an_order_filled_later(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger_with_signal(tmp)
            gateway = LaterFillGateway()
            service = TradingService(
                ledger=ledger,
                gateway=gateway,
                lock_path=Path(tmp) / "executor.lock",
            )
            service.execute_strategy("new")
            attempt = ledger.list_attempts("new")[0]
            self.assertEqual("LIVE", ledger.get_attempt(attempt.attempt_id).state)

            gateway.later_filled = True
            results = service.reconcile_open_attempts()

            self.assertEqual(1, len(results))
            self.assertEqual(ReconcileStatus.FILLED, results[0][1].status)
            self.assertEqual("FILLED", ledger.get_attempt(attempt.attempt_id).state)
            protection = ledger.get_protection(attempt.attempt_id)
            self.assertIsNotNone(protection)
            self.assertEqual("ACTIVE", protection.state)
            self.assertEqual("1", protection.covered_size)

            gateway.protection_triggered = True
            closed = service.reconcile_open_attempts()

            self.assertEqual(ReconcileStatus.CLOSED, closed[0][1].status)
            self.assertEqual("CLOSED", ledger.get_attempt(attempt.attempt_id).state)
            self.assertEqual(
                "TRIGGERED", ledger.get_protection(attempt.attempt_id).state
            )
            self.assertEqual((), ledger.list_reconcilable_attempts())

    def test_one_transient_reconciliation_failure_then_recovery_stays_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger_with_signal(tmp)
            gateway = TransientOrderGateway()
            service = TradingService(
                ledger=ledger,
                gateway=gateway,
                lock_path=Path(tmp) / "executor.lock",
            )
            service.execute_strategy("new")
            for notification in ledger.list_pending_notifications():
                ledger.mark_notification_sent(notification.notification_id)

            gateway.fail_order_reads = True
            first = service.reconcile_open_attempts()
            gateway.fail_order_reads = False
            recovered = service.reconcile_open_attempts()

            self.assertEqual(ReconcileStatus.NEEDS_REVIEW, first[0][1].status)
            self.assertEqual(ReconcileStatus.LIVE, recovered[0][1].status)
            self.assertEqual((), ledger.list_pending_notifications())

    def test_three_consecutive_reconciliation_failures_send_one_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger_with_signal(tmp)
            gateway = TransientOrderGateway()
            service = TradingService(
                ledger=ledger,
                gateway=gateway,
                lock_path=Path(tmp) / "executor.lock",
            )
            service.execute_strategy("new")
            for notification in ledger.list_pending_notifications():
                ledger.mark_notification_sent(notification.notification_id)
            gateway.fail_order_reads = True

            service.reconcile_open_attempts()
            service.reconcile_open_attempts()
            self.assertEqual((), ledger.list_pending_notifications())
            service.reconcile_open_attempts()

            pending = ledger.list_pending_notifications()
            self.assertEqual(1, len(pending))
            self.assertIn("已合并", pending[0].message)

    def test_same_transient_reconciliation_error_is_aggregated_then_recovers_once(self):
        """A broken exchange connection must not produce one WeChat message per order."""
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger_with_signal(tmp)
            service = TradingService(
                ledger=ledger,
                gateway=ServiceGateway(),
                lock_path=Path(tmp) / "executor.lock",
            )
            attempts = (
                SimpleNamespace(
                    attempt_id="aave-long",
                    strategy_id="new",
                    symbol="AAVE-USDT-SWAP",
                    direction="long",
                    updated_at="2026-09-02T00:00:00+00:00",
                ),
                SimpleNamespace(
                    attempt_id="link-short",
                    strategy_id="new",
                    symbol="LINK-USDT-SWAP",
                    direction="short",
                    updated_at="2026-09-02T00:00:00+00:00",
                ),
            )
            timeout = ReconcileResult(
                ReconcileStatus.NEEDS_REVIEW,
                "订单详情查询异常 (ConnectTimeout)",
            )
            for _ in range(3):
                for attempt in attempts:
                    service._maybe_queue_reconciliation_alert(
                        attempt=attempt,
                        result=timeout,
                        current_state="LIVE",
                        filled_size="0",
                    )

            pending = ledger.list_pending_notifications()
            self.assertEqual(1, len(pending))
            self.assertIn("已合并", pending[0].message)

            ledger.mark_notification_sent(pending[0].notification_id)
            live = ReconcileResult(ReconcileStatus.LIVE)
            for attempt in attempts:
                service._maybe_queue_reconciliation_alert(
                    attempt=attempt,
                    result=live,
                    current_state="LIVE",
                    filled_size="0",
                )

            recovery = ledger.list_pending_notifications()
            self.assertEqual(1, len(recovery))
            self.assertIn("恢复", recovery[0].message)

    def test_canceled_superseded_order_reconciliation_stays_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger_with_signal(tmp)
            gateway = ServiceGateway()
            service = TradingService(
                ledger=ledger,
                gateway=gateway,
                lock_path=Path(tmp) / "executor.lock",
            )
            service.execute_strategy("new")
            for notification in ledger.list_pending_notifications():
                ledger.mark_notification_sent(notification.notification_id)
            attempt = ledger.list_attempts("new")[0]
            replacement = TradeSignal(
                symbol="BTC-USDT-SWAP",
                direction="short",
                entry_low=68000,
                entry_high=68000,
                entry_price=68000,
                stop=69000,
                target=67000,
            )
            ledger.record_strategy(
                strategy_id="replacement",
                symbol=replacement.symbol,
                source_chat_id=-100,
                source_sender_id=1818,
                source_message_id=3,
                raw_text="replacement",
                directions=("short",),
                signals=(replacement,),
            )

            def canceled_order(symbol, *, ord_id=None, cl_ord_id=None):
                return {
                    "code": "0",
                    "data": [{
                        "instId": symbol,
                        "ordId": ord_id,
                        "clOrdId": attempt.cl_ord_id,
                        "state": "canceled",
                        "accFillSz": "0",
                    }],
                }

            gateway.order = canceled_order
            result = service.reconcile_open_attempts()

            self.assertEqual(ReconcileStatus.CANCELED, result[0][1].status)
            self.assertEqual((), ledger.list_pending_notifications())

    def test_unknown_order_retries_only_after_full_missing_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger_with_signal(tmp)
            attempt = ledger.list_attempts("new")[0]
            ledger.transition_attempt(
                attempt.attempt_id,
                expected_states=("READY",),
                new_state="UNKNOWN",
                requested_size="2",
            )
            gateway = ServiceGateway()
            service = TradingService(
                ledger=ledger,
                gateway=gateway,
                lock_path=Path(tmp) / "executor.lock",
                now_provider=self._after_grace,
            )

            reconciliation, execution = service.recover_attempt(attempt.attempt_id)

            self.assertEqual(ReconcileStatus.MISSING_CONFIRMED, reconciliation.status)
            self.assertIsNotNone(execution)
            self.assertEqual(ExecutionStatus.SUBMITTED, execution.status)
            recovered = ledger.get_attempt(attempt.attempt_id)
            self.assertEqual(1, recovered.retry_count)
            self.assertEqual("LIVE", recovered.state)
            self.assertEqual(1, len(gateway.placed))

    def test_background_recovery_retries_only_a_confirmed_missing_active_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger_with_signal(tmp)
            attempt = ledger.list_attempts("new")[0]
            ledger.transition_attempt(
                attempt.attempt_id,
                expected_states=("READY",),
                new_state="UNKNOWN",
                requested_size="0.76",
            )
            gateway = ServiceGateway()
            service = TradingService(
                ledger=ledger,
                gateway=gateway,
                lock_path=Path(tmp) / "executor.lock",
                now_provider=self._after_grace,
            )

            service.reconcile_open_attempts()

            recovered = ledger.get_attempt(attempt.attempt_id)
            self.assertEqual("LIVE", recovered.state)
            self.assertEqual(1, recovered.retry_count)
            self.assertEqual(1, len(gateway.placed))

    def test_restart_reconciles_uncertain_old_cancel_before_new_order(self):
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
                new_state="CANCEL_UNKNOWN",
                ord_id="OLD-1",
                requested_size="1",
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
            gateway = ServiceGateway()
            def order_after_restart(symbol, *, ord_id=None, cl_ord_id=None):
                if ord_id == "OLD-1":
                    return {
                        "code": "0",
                        "data": [{
                            "instId": symbol,
                            "ordId": "OLD-1",
                            "clOrdId": old.cl_ord_id,
                            "state": "canceled",
                            "accFillSz": "0",
                        }],
                    }
                return {
                    "code": "0",
                    "data": [{
                        "instId": symbol,
                        "ordId": ord_id,
                        "clOrdId": cl_ord_id,
                        "state": "live",
                        "accFillSz": "0",
                    }],
                }

            gateway.order = order_after_restart
            service = TradingService(
                ledger=ledger,
                gateway=gateway,
                lock_path=Path(tmp) / "executor.lock",
            )

            outcome = service.execute_strategy("new")

            self.assertEqual(ExecutionStatus.SUBMITTED, outcome.status)
            self.assertEqual("CANCELED", ledger.get_attempt(old.attempt_id).state)
            self.assertEqual(1, len(gateway.placed))

    def test_superseded_missing_attempt_is_never_retried(self):
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
                new_state="UNKNOWN",
                requested_size="1",
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
            gateway = ServiceGateway()
            service = TradingService(
                ledger=ledger,
                gateway=gateway,
                lock_path=Path(tmp) / "executor.lock",
                now_provider=self._after_grace,
            )

            reconciliation, execution = service.recover_attempt(old.attempt_id)

            self.assertEqual(ReconcileStatus.MISSING_CONFIRMED, reconciliation.status)
            self.assertIsNone(execution)
            self.assertEqual(
                "MISSING_CONFIRMED", ledger.get_attempt(old.attempt_id).state
            )
            self.assertEqual(0, ledger.get_attempt(old.attempt_id).retry_count)
            self.assertEqual([], gateway.placed)


if __name__ == "__main__":
    unittest.main()
