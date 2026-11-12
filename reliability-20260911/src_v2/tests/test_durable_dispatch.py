import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src_v2.runtime import RuntimeMode
from src_v2.tests import test_runtime
from src_v2.service import TradingService


class DurableDispatchTests(unittest.TestCase):
    def setup_runtime(self, directory):
        runtime, ledger, gateway = test_runtime.StrategyRuntimeTests()._components(directory, RuntimeMode.LIVE)
        runtime.defer_execution = True
        runtime.adapter.intake.dispatch_enabled = True
        return runtime, ledger, gateway

    def message(self, runtime, message_id=1):
        return test_runtime.StrategyRuntimeTests._message(runtime, message_id)

    def test_callback_persists_job_without_calling_exchange_then_worker_submits_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            r,l,g = self.setup_runtime(tmp)
            msg = self.message(r)
            self.assertIsNone(msg.execution)
            self.assertEqual([],g.placed)
            self.assertTrue(l.strategy_has_authenticated_ready_ingest(msg.strategy_id))
            self.assertEqual('PENDING',l.get_execution_job(msg.strategy_id)['state'])
            r.service.dispatch_pending()
            self.message(r)
            r.service.dispatch_pending()
            self.assertEqual(1,len(g.placed))
            self.assertEqual('DONE',l.get_execution_job(msg.strategy_id)['state'])

    def test_transient_read_failure_recovers_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            r,l,g = self.setup_runtime(tmp); msg=self.message(r)
            now=datetime.now(timezone.utc)
            with patch.object(g,'positions',side_effect=TimeoutError()):
                r.service.dispatch_pending(now=now)
            self.assertEqual('RETRY',l.get_execution_job(msg.strategy_id)['state'])
            restarted=TradingService(ledger=l,gateway=g,lock_path=r.service.lock_path,require_source_attestation=True)
            restarted.dispatch_pending(now=now+timedelta(seconds=31))
            self.assertEqual(1,len(g.placed))
            self.assertEqual('DONE',l.get_execution_job(msg.strategy_id)['state'])

    def test_accepted_job_waiting_in_queue_keeps_its_execution_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            r,l,g=self.setup_runtime(tmp); msg=self.message(r)
            r.service.dispatch_pending(now=datetime.now(timezone.utc)+timedelta(minutes=6))
            self.assertEqual(1,len(g.placed))
            self.assertEqual('DONE',l.get_execution_job(msg.strategy_id)['state'])

    def test_old_ready_records_without_jobs_are_not_backfilled(self):
        with tempfile.TemporaryDirectory() as tmp:
            r,l,g=self.setup_runtime(tmp); r.adapter.intake.dispatch_enabled=False
            self.message(r); r.service.dispatch_pending()
            self.assertEqual([],g.placed)

    def test_ambiguous_submission_does_not_duplicate_on_worker_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            r,l,g=self.setup_runtime(tmp); msg=self.message(r)
            with patch.object(g,'place_order',side_effect=TimeoutError()) as send:
                r.service.dispatch_pending()
                r.service.dispatch_pending(now=datetime.now(timezone.utc)+timedelta(seconds=31))
                self.assertEqual(1,send.call_count)
            self.assertEqual('RECONCILING',l.get_execution_job(msg.strategy_id)['state'])

    def test_record_and_audit_are_atomic_when_job_insert_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            r,l,g=self.setup_runtime(tmp)
            with l._connection() as c:
                c.execute("CREATE TRIGGER fail_job BEFORE INSERT ON execution_jobs BEGIN SELECT RAISE(ABORT,'injected'); END")
                c.commit()
            with self.assertRaises(Exception): self.message(r)
            self.assertIsNone(l.get_active_strategy('BTC-USDT-SWAP'))
            self.assertEqual((),l.list_ingest_events())

    def test_pause_does_not_lose_job_and_duplicate_does_not_extend_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            r,l,g=self.setup_runtime(tmp);msg=self.message(r)
            deadline=l.get_execution_job(msg.strategy_id)['deadline']
            l.pause_automation('user pause')
            r.service.dispatch_pending()
            self.assertEqual([],g.placed)
            self.assertEqual('PENDING',l.get_execution_job(msg.strategy_id)['state'])
            l.resume_automation('user resume')
            self.message(r)
            self.assertEqual(deadline,l.get_execution_job(msg.strategy_id)['deadline'])
            r.service.dispatch_pending()
            self.assertEqual(1,len(g.placed))

    def test_superseded_pending_job_is_not_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            r,l,g=self.setup_runtime(tmp);old=self.message(r,1);new=self.message(r,2)
            r.service.dispatch_pending()
            self.assertEqual('SUPERSEDED',l.get_execution_job(old.strategy_id)['state'])
            self.assertEqual('DONE',l.get_execution_job(new.strategy_id)['state'])
            self.assertEqual(1,len(g.placed))

    def test_crash_after_marking_running_is_recovered_by_new_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            r,l,g=self.setup_runtime(tmp);msg=self.message(r)
            l.update_execution_job(msg.strategy_id,state='RUNNING',now=datetime.now(timezone.utc).isoformat(),started=True)
            restarted=TradingService(ledger=l,gateway=g,lock_path=r.service.lock_path,require_source_attestation=True)
            restarted.dispatch_pending()
            self.assertEqual('DONE',l.get_execution_job(msg.strategy_id)['state'])
            self.assertEqual(1,len(g.placed))

    def test_simultaneous_workers_submit_once(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as tmp:
            r,l,g=self.setup_runtime(tmp);msg=self.message(r)
            with ThreadPoolExecutor(max_workers=2) as pool:
                a=pool.submit(r.service.dispatch_pending);b=pool.submit(r.service.dispatch_pending)
                a.result(timeout=3);b.result(timeout=3)
            self.assertEqual('DONE',l.get_execution_job(msg.strategy_id)['state'])
            self.assertEqual(1,len(g.placed))

    def test_read_only_status_does_not_create_or_mutate_database(self):
        from pathlib import Path
        from src_v2.status import read_status
        with tempfile.TemporaryDirectory() as tmp:
            r,l,g=self.setup_runtime(tmp);msg=self.message(r)
            before=Path(l.path).read_bytes()
            report=read_status(l.path)
            self.assertEqual('PENDING',report['jobs'][0]['state'])
            self.assertEqual(before,Path(l.path).read_bytes())
            missing=Path(tmp)/'absent.sqlite3'
            with self.assertRaises(FileNotFoundError):read_status(missing)
            self.assertFalse(missing.exists())

    def test_one_direction_success_then_read_failure_resumes_only_remaining_direction(self):
        from decimal import Decimal
        with tempfile.TemporaryDirectory() as tmp:
            r,l,g=self.setup_runtime(tmp)
            event=dict(chat_id=test_runtime.CHAT_ID,sender_id=test_runtime.SENDER_ID,sender_username='trusted',message_id=10,
                message_date=datetime.now(timezone.utc),text='HYPE，83.5-84附近区间震荡。')
            msg=r.on_new_message(**event)
            now=datetime.now(timezone.utc)
            with patch.object(g,'current_price',side_effect=[Decimal('83.75'),Decimal('83.75'),TimeoutError()]):
                r.service.dispatch_pending(now=now)
            self.assertEqual(1,len(g.placed))
            self.assertEqual('RETRY',l.get_execution_job(msg.strategy_id)['state'])
            with patch.object(g,'current_price',return_value=Decimal('83.75')):
                r.service.dispatch_pending(now=now+timedelta(seconds=31))
            self.assertEqual(2,len(g.placed))
            self.assertEqual({'buy','sell'},{p['side'] for p in g.placed})
            self.assertEqual('DONE',l.get_execution_job(msg.strategy_id)['state'])
            self.assertTrue(all(a.ord_id for a in l.list_attempts(msg.strategy_id)))

    def test_invalid_newer_schema_is_not_modified(self):
        import sqlite3
        from pathlib import Path
        from src_v2.ledger import Ledger
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'future.db'
            with sqlite3.connect(p) as c:c.execute('PRAGMA user_version=999')
            before=p.read_bytes()
            with self.assertRaises(RuntimeError):Ledger(p)
            self.assertEqual(before,p.read_bytes())

    def test_receipt_and_notification_commit_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            r,l,g=self.setup_runtime(tmp);msg=self.message(r)
            with l._connection() as c:
                c.execute("CREATE TRIGGER fail_notice BEFORE INSERT ON notifications BEGIN SELECT RAISE(ABORT,'injected'); END")
                c.commit()
            with self.assertRaises(Exception): r.service.dispatch_pending()
            self.assertEqual('RUNNING',l.get_execution_job(msg.strategy_id)['state'])
            self.assertEqual(1,len(g.placed))
            with l._connection() as c:
                c.execute('DROP TRIGGER fail_notice');c.commit()
            r.service.dispatch_pending()
            self.assertEqual(1,len(g.placed))
            self.assertEqual('DONE',l.get_execution_job(msg.strategy_id)['state'])
            self.assertEqual(1,len(l.list_pending_notifications()))

    def test_three_retries_after_initial_failure_can_recover(self):
        with tempfile.TemporaryDirectory() as tmp:
            r,l,g=self.setup_runtime(tmp);msg=self.message(r)
            now=datetime.now(timezone.utc)
            with patch.object(g,'positions',side_effect=[TimeoutError(),TimeoutError(),TimeoutError(),[]]) as query:
                for seconds in (0,30,90,210):
                    r.service.dispatch_pending(now=now+timedelta(seconds=seconds))
                self.assertEqual(4,query.call_count)
            self.assertEqual('DONE',l.get_execution_job(msg.strategy_id)['state'])
            self.assertEqual(4,l.get_execution_job(msg.strategy_id)['attempts'])
            self.assertEqual(1,len(g.placed))

    def test_three_failed_retries_keep_durable_backoff_without_tight_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            r,l,g=self.setup_runtime(tmp);msg=self.message(r)
            now=datetime.now(timezone.utc)
            with patch.object(g,'positions',side_effect=TimeoutError()) as query:
                for seconds in (0,30,90,210,250):
                    r.service.dispatch_pending(now=now+timedelta(seconds=seconds))
                self.assertEqual(4,query.call_count)
            job=l.get_execution_job(msg.strategy_id)
            self.assertEqual('RETRY',job['state'])
            self.assertIn('持仓查询失败',job['last_error'])
            self.assertEqual([],g.placed)
            self.assertTrue(any('重试3次' in n.message for n in l.list_pending_notifications()))
