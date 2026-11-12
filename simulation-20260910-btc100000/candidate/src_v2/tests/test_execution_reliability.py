"""Offline regressions for September 8 intake/execution failures."""
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from src_v2.executor import ExecutionStatus
from src_v2.okx_gateway import GatewayError, OKXGateway
from src_v2.runtime import RuntimeMode
from src_v2.tests import test_runtime


class ExecutionReliabilityTests(unittest.TestCase):
    def test_new_strategy_waits_for_in_process_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = test_runtime.StrategyRuntimeTests()
            runtime, ledger, gateway = helper._components(tmp, RuntimeMode.LIVE)
            entered, release, submitting = threading.Event(), threading.Event(), threading.Event()
            original = ledger.list_reconcilable_attempts

            def held_reconciliation():
                entered.set()
                if not release.wait(3):
                    raise AssertionError("test coordination timed out")
                return original()

            def new_message():
                submitting.set()
                return helper._message(runtime)

            with patch.object(ledger, 'list_reconcilable_attempts', held_reconciliation):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    watcher = pool.submit(runtime.service.reconcile_open_attempts)
                    self.assertTrue(entered.wait(3))
                    incoming = pool.submit(new_message)
                    self.assertTrue(submitting.wait(3))
                    try:
                        # A queued operation must not fail just because this service
                        # is currently reconciling an earlier order.
                        with self.assertRaises(TimeoutError):
                            incoming.result(timeout=0.1)
                    finally:
                        release.set()
                    watcher.result(timeout=3)
                    result = incoming.result(timeout=3)
            self.assertEqual(ExecutionStatus.SUBMITTED, result.execution.status)
            self.assertEqual(1, len(gateway.placed))
            self.assertEqual(0, helper._message(runtime).execution.placed)

    def test_pending_read_recovery_reaches_one_order_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = test_runtime.StrategyRuntimeTests()
            runtime, ledger, gateway = helper._components(tmp, RuntimeMode.LIVE)
            trade = Mock()
            trade.get_order_list.side_effect = [TimeoutError(), {'code': '0', 'data': []}]
            reads = OKXGateway(account_api=Mock(), trade_api=trade, market_api=Mock(), public_api=Mock())
            gateway.pending_orders = reads.pending_orders
            with patch('time.sleep'):
                result = helper._message(runtime)
                duplicate = helper._message(runtime)
            self.assertEqual(ExecutionStatus.SUBMITTED, result.execution.status)
            self.assertEqual(1, len(gateway.placed))
            self.assertEqual(0, duplicate.execution.placed)
            self.assertEqual("LIVE", ledger.list_attempts(result.strategy_id)[0].state)

    def test_other_executor_still_cannot_enter_process_lock(self):
        from src_v2.process_lock import ExclusiveProcessLock, SingleInstanceBusy
        with tempfile.TemporaryDirectory() as tmp:
            helper = test_runtime.StrategyRuntimeTests()
            runtime, ledger, gateway = helper._components(tmp, RuntimeMode.LIVE)
            with ExclusiveProcessLock(runtime.service.lock_path):
                with self.assertRaises(SingleInstanceBusy):
                    helper._message(runtime)
            self.assertEqual([], gateway.placed)

    def test_pending_read_recovers_before_returning_orders(self):
        trade = Mock()
        trade.get_order_list.side_effect = [TimeoutError(), {'code': '1'}, {'code': '0', 'data': [{'ordId': 'existing'}]}]
        gateway = OKXGateway(account_api=Mock(), trade_api=trade, market_api=Mock(), public_api=Mock())
        with patch('time.sleep'):
            self.assertEqual([{'ordId': 'existing'}], gateway.pending_orders('ETH-USDT-SWAP'))
        self.assertEqual(3, trade.get_order_list.call_count)
        trade.place_order.assert_not_called()
        trade.cancel_order.assert_not_called()

    def test_exhausted_pending_read_is_not_treated_as_empty(self):
        trade = Mock()
        trade.get_order_list.side_effect = TimeoutError()
        gateway = OKXGateway(account_api=Mock(), trade_api=trade, market_api=Mock(), public_api=Mock())
        with patch('time.sleep'), self.assertRaises(GatewayError):
            gateway.pending_orders('ETH-USDT-SWAP')
        self.assertEqual(3, trade.get_order_list.call_count)
        trade.place_order.assert_not_called()
