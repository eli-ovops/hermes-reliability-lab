"""Sept 11 incident reproductions. All gateways, clocks and senders are offline."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src_v2.executor import ExecutionStatus
from src_v2.ledger import Ledger
from src_v2.notifications import NotificationDispatcher
from src_v2.parser import TradeSignal
from src_v2.service import TradingService
from src_v2.tests import test_durable_dispatch, test_service
from src_v2.wechat_sender import WeChatSender


class ReliabilityRegressionTests(unittest.TestCase):
    def test_slow_reads_do_not_cut_off_third_retry_after_five_minutes(self):
        fixture = test_durable_dispatch.DurableDispatchTests()
        with tempfile.TemporaryDirectory() as tmp:
            runtime, ledger, gateway = fixture.setup_runtime(tmp)
            msg = fixture.message(runtime)
            now = datetime.now(timezone.utc)
            with patch.object(gateway, 'positions', side_effect=[TimeoutError(), TimeoutError(), TimeoutError(), []]) as query:
                for seconds in (0, 180, 400, 700):
                    runtime.service.dispatch_pending(now=now + timedelta(seconds=seconds))
            self.assertEqual(4, query.call_count)
            self.assertEqual('DONE', ledger.get_execution_job(msg.strategy_id)['state'])
            self.assertEqual(4, ledger.get_execution_job(msg.strategy_id)['attempts'])
            self.assertEqual(1, len(gateway.placed))
            self.assertTrue(gateway.placed[0]['attachAlgoOrds'])

    def test_terminal_historical_jobs_are_not_revived(self):
        fixture = test_durable_dispatch.DurableDispatchTests()
        for terminal in ('EXPIRED', 'BLOCKED'):
            with self.subTest(terminal=terminal), tempfile.TemporaryDirectory() as tmp:
                runtime, ledger, gateway = fixture.setup_runtime(tmp)
                msg = fixture.message(runtime)
                now = datetime.now(timezone.utc).isoformat()
                ledger.update_execution_job(msg.strategy_id, state=terminal, now=now, error='historical')
                runtime.service.dispatch_pending()
                self.assertEqual([], gateway.placed)
                self.assertEqual(terminal, ledger.get_execution_job(msg.strategy_id)['state'])

    def test_long_outage_recovers_after_fast_retries_and_process_restart(self):
        fixture = test_durable_dispatch.DurableDispatchTests()
        with tempfile.TemporaryDirectory() as tmp:
            runtime, ledger, gateway = fixture.setup_runtime(tmp)
            msg = fixture.message(runtime)
            now = datetime.now(timezone.utc)
            with patch.object(gateway, 'positions', side_effect=TimeoutError()):
                for seconds in (0, 30, 90, 210):
                    runtime.service.dispatch_pending(now=now+timedelta(seconds=seconds))
            self.assertEqual('RETRY', ledger.get_execution_job(msg.strategy_id)['state'])
            restarted = TradingService(ledger=Ledger(ledger.path), gateway=gateway,
                lock_path=runtime.service.lock_path, require_source_attestation=True)
            restarted.dispatch_pending(now=now+timedelta(seconds=400))
            self.assertEqual([], gateway.placed)
            restarted.dispatch_pending(now=now+timedelta(seconds=511))
            self.assertEqual('DONE', ledger.get_execution_job(msg.strategy_id)['state'])
            self.assertEqual(5, ledger.get_execution_job(msg.strategy_id)['attempts'])
            self.assertEqual(1, len(gateway.placed))

    def test_first_failure_survives_running_and_terminal_transitions(self):
        fixture = test_durable_dispatch.DurableDispatchTests()
        with tempfile.TemporaryDirectory() as tmp:
            runtime, ledger, _ = fixture.setup_runtime(tmp)
            msg = fixture.message(runtime)
            now = datetime.now(timezone.utc).isoformat()
            ledger.update_execution_job(msg.strategy_id, state='RETRY', now=now, error='positions: ConnectTimeout')
            ledger.update_execution_job(msg.strategy_id, state='RUNNING', now=now, started=True)
            self.assertEqual('positions: ConnectTimeout', ledger.get_execution_job(msg.strategy_id)['last_error'])
            ledger.update_execution_job(msg.strategy_id, state='BLOCKED', now=now, error='retry exhausted')
            reopened = Ledger(ledger.path)
            with reopened._connection() as c:
                rows = c.execute("SELECT detail FROM audit_events WHERE strategy_id=? AND event_type='EXECUTION_JOB_TRANSITION' ORDER BY event_id", (msg.strategy_id,)).fetchall()
            history = [json.loads(r[0]) for r in rows]
            self.assertEqual(['RETRY', 'RUNNING', 'BLOCKED'], [r['to'] for r in history])
            self.assertEqual('positions: ConnectTimeout', history[0]['reason'])
            self.assertEqual(1, history[-1]['attempts'])

    def old_filled_and_new(self, tmp):
        ledger = Ledger(Path(tmp) / 'ledger.sqlite3')
        signal = TradeSignal(symbol='BTC-USDT-SWAP', direction='long', entry_low=65000,
                             entry_high=65000, entry_price=65000, stop=64000, target=67000)
        for sid, mid in [('old', 1), ('new', 2)]:
            ledger.record_strategy(strategy_id=sid, symbol=signal.symbol, source_chat_id=-100,
                                   source_sender_id=1818, source_message_id=mid, raw_text=sid,
                                   directions=('long',), signals=(signal,))
            if sid == 'old':
                old = ledger.list_attempts('old')[0]
                ledger.transition_attempt(old.attempt_id, expected_states=('READY',), new_state='FILLED',
                                          ord_id='OLD-1', requested_size='1', filled_size='1')
                ledger.record_protection(old.attempt_id, algo_id='ALGO-OLD', covered_size='1', state='ACTIVE')
        gateway = test_service.ServiceGateway()
        normal_order = gateway.order
        def read_order(symbol, *, ord_id=None, cl_ord_id=None):
            if ord_id == 'OLD-1':
                return {'code':'0', 'data':[{'instId':symbol, 'ordId':'OLD-1', 'clOrdId':old.cl_ord_id,
                    'state':'filled', 'accFillSz':'1'}]}
            return normal_order(symbol, ord_id=ord_id, cl_ord_id=cl_ord_id)
        gateway.order = read_order
        gateway.algo_history_response = lambda symbol, **kw: {'code':'0', 'data':[{
            'instId':symbol, 'algoId':'ALGO-OLD', 'algoClOrdId':old.cl_ord_id+'P', 'posSide':'long',
            'ordType':'oco', 'state':'effective', 'sz':'1'}]}
        service = TradingService(ledger=ledger, gateway=gateway, lock_path=Path(tmp)/'lock')
        return ledger, gateway, service, old

    def test_filled_old_entry_with_terminal_protection_and_zero_position_does_not_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger, gateway, service, old = self.old_filled_and_new(tmp)
            result = service.execute_strategy('new')
            self.assertEqual(ExecutionStatus.SUBMITTED, result.status)
            self.assertEqual('CLOSED', ledger.get_attempt(old.attempt_id).state)
            self.assertEqual('TRIGGERED', ledger.get_protection(old.attempt_id).state)
            self.assertEqual(1, len(gateway.placed))

    def test_old_entry_read_timeout_is_retryable_not_permanently_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, gateway, service, _ = self.old_filled_and_new(tmp)
            with patch.object(gateway, 'order', side_effect=TimeoutError()):
                result = service.execute_strategy('new')
            self.assertEqual(ExecutionStatus.RETRYABLE, result.status)
            self.assertIn('TimeoutError', result.reason)
            self.assertEqual([], gateway.placed)

    def test_existing_old_position_still_blocks_a_new_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, gateway, service, _ = self.old_filled_and_new(tmp)
            gateway.positions_response = lambda symbol: {'code':'0', 'data':[{
                'instId':symbol, 'posSide':'long', 'pos':'1'}]}
            result = service.execute_strategy('new')
            self.assertEqual(ExecutionStatus.NEEDS_REVIEW, result.status)
            self.assertEqual([], gateway.placed)

    def test_history_query_outage_does_not_latch_a_false_protection_pause(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger, gateway, service, old = self.old_filled_and_new(tmp)
            with patch.object(gateway, 'algo_history_response', side_effect=TimeoutError()):
                first = service.execute_strategy('new')
            self.assertEqual(ExecutionStatus.RETRYABLE, first.status)
            self.assertIsNone(ledger.automation_pause_reason())
            self.assertEqual([], gateway.placed)
            second = service.execute_strategy('new')
            self.assertEqual(ExecutionStatus.SUBMITTED, second.status)
            self.assertEqual('CLOSED', ledger.get_attempt(old.attempt_id).state)

    def test_first_direction_already_closed_does_not_block_remaining_direction_retry(self):
        from decimal import Decimal
        fixture = test_durable_dispatch.DurableDispatchTests()
        with tempfile.TemporaryDirectory() as tmp:
            runtime, ledger, gateway = fixture.setup_runtime(tmp)
            from src_v2.tests.test_runtime import CHAT_ID, SENDER_ID
            msg = runtime.on_new_message(chat_id=CHAT_ID, sender_id=SENDER_ID, sender_username='trusted',
                message_id=20, message_date=datetime.now(timezone.utc), text='HYPE，83.5-84附近区间震荡。')
            now = datetime.now(timezone.utc)
            with patch.object(gateway, 'current_price', side_effect=[Decimal('83.75'), Decimal('83.75'), TimeoutError()]):
                runtime.service.dispatch_pending(now=now)
            filled = next(a for a in ledger.list_attempts(msg.strategy_id) if a.ord_id)
            ledger.transition_attempt(filled.attempt_id, expected_states=('LIVE',), new_state='CLOSED')
            with patch.object(gateway, 'current_price', return_value=Decimal('83.75')):
                runtime.service.dispatch_pending(now=now+timedelta(seconds=31))
            self.assertEqual('DONE', ledger.get_execution_job(msg.strategy_id)['state'])
            self.assertEqual(2, len(gateway.placed))
            self.assertEqual({'buy','sell'}, {p['side'] for p in gateway.placed})

    def test_old_backoff_does_not_hide_a_new_notification_behind_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp)/'ledger.sqlite3')
            first = ledger.enqueue_notification(dedupe_key='old', kind='REVIEW_REQUIRED', message='old')
            ledger.mark_notification_failed(first.notification_id, 'offline')
            ledger.enqueue_notification(dedupe_key='new', kind='REVIEW_REQUIRED', message='new')
            sent = []
            result = NotificationDispatcher(ledger=ledger).flush(sent.append, limit=1, backoff_base=3600, max_attempts=1)
            self.assertEqual((1, 0), result)
            self.assertEqual(['new'], sent)

    def test_delivery_error_retains_safe_backend_code_without_echoing_secrets(self):
        response = SimpleNamespace(returncode=1, stdout=json.dumps({
            'error':'Weixin send failed: iLink sendmessage error: ret=-14 errcode=-14 errmsg=session expired token=do-not-print'
        }), stderr='Authorization: Bearer do-not-print')
        with patch('src_v2.wechat_sender.subprocess.run', return_value=response):
            with self.assertRaises(RuntimeError) as caught:
                WeChatSender().send('notice')
        text = str(caught.exception)
        self.assertIn('errcode=-14', text)
        self.assertIn('会话', text)
        self.assertNotIn('do-not-print', text)

    def test_rate_limit_backoff_is_shared_by_outbox_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp)/'ledger.sqlite3')
            for n in range(3):
                ledger.enqueue_notification(dedupe_key=str(n), kind='REVIEW_REQUIRED', message=str(n))
            now = datetime.now(timezone.utc)
            calls = []
            def limited(message):
                calls.append(message)
                raise RuntimeError('WECHAT_RATE_LIMITED 微信通道限流')
            first = NotificationDispatcher(ledger=ledger).flush(limited, now=now)
            self.assertEqual((0, 1), first)
            restarted = NotificationDispatcher(ledger=Ledger(ledger.path))
            self.assertEqual((0, 0), restarted.flush(limited, now=now+timedelta(seconds=31)))
            self.assertEqual(1, len(calls))
            self.assertEqual((0, 1), restarted.flush(limited, now=now+timedelta(seconds=61)))
            self.assertEqual((0, 0), restarted.flush(limited, now=now+timedelta(seconds=130)))
            sent = []
            self.assertEqual((3, 0), restarted.flush(sent.append, now=now+timedelta(seconds=182)))
            self.assertEqual(3, len(sent))
            self.assertIsNone(ledger.automation_pause_reason())


if __name__ == '__main__':
    unittest.main()
