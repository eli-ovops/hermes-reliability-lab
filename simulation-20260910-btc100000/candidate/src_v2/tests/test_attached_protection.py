import copy
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

from src_v2.attached_protection import attachment, attachment_matches, algo_matches
from src_v2.ledger import Ledger
from src_v2.parser import TradeSignal
from src_v2.reconciler import Reconciler, ReconcileStatus
from src_v2.protection import ProtectionManager, ProtectionStatus

class NativeProtectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Ledger(Path(self.tmp.name)/'ledger.db')
        self.signal = TradeSignal('BTC-USDT-SWAP','long',65000,65000,65000,64000,67000)
        self.ledger.record_strategy(strategy_id='new',symbol=self.signal.symbol,source_chat_id=-100,source_sender_id=1818,source_message_id=1,raw_text='signal',directions=('long',),signals=(self.signal,))
        self.attempt = self.ledger.list_attempts('new')[0]
        self.expected = attachment(self.attempt.cl_ord_id,64000,67000)
        self.ledger.record_attachment_intent(self.attempt.attempt_id,self.expected)
        self.ledger.transition_attempt(self.attempt.attempt_id,expected_states=('READY',),new_state='SUBMITTED',ord_id='ENTRY',requested_size='3')
        self.order = dict(instId=self.signal.symbol,ordId='ENTRY',clOrdId=self.attempt.cl_ord_id,state='live',accFillSz='0',attachAlgoOrds=[copy.deepcopy(self.expected)])
        self.gateway = Mock()
        self.gateway.order.side_effect = lambda *a, **k: dict(code='0',data=[copy.deepcopy(self.order)])
        self.gateway.instrument.return_value = {'tickSz':'0.1'}
        self.gateway.algo_orders_response.return_value = {'code':'0','data':[]}
        self.gateway.positions_response.return_value = {'code':'0','data':[]}
        self.gateway.algo_history_response.return_value = {'code':'0','data':[]}

    def reconcile(self):
        return Reconciler(ledger=self.ledger,gateway=self.gateway).reconcile(self.attempt.attempt_id)

    def active(self,size='3'):
        return dict(instId=self.signal.symbol,algoId='ALGO',algoClOrdId=self.expected['attachAlgoClOrdId'],ordType='oco',state='live',posSide='long',side='sell',sz=size,**{k:v for k,v in self.expected.items() if k!='attachAlgoClOrdId'})

    def test_bare_entry_is_incident_not_success(self):
        self.order['attachAlgoOrds']=[]
        self.assertEqual(ReconcileStatus.NEEDS_REVIEW,self.reconcile().status)
        self.assertIsNotNone(self.ledger.automation_pause_reason())

    def test_complete_attachment_is_accepted_without_standalone_order(self):
        self.assertEqual(ReconcileStatus.LIVE,self.reconcile().status)
        self.gateway.place_protection.assert_not_called()

    def test_wrong_stop_or_missing_tp_or_failure_code_is_rejected(self):
        for field,value in [('slTriggerPx','63000'),('tpTriggerPx',''),('failCode','51020'),('slTriggerPxType','last'),('slOrdPx','63900')]:
            with self.subTest(field=field):
                order=copy.deepcopy(self.order);order['attachAlgoOrds'][0][field]=value
                self.assertFalse(attachment_matches(order,self.expected))

    def test_full_fill_adopts_native_child_and_never_duplicates(self):
        self.order.update(state='filled',accFillSz='3')
        self.gateway.algo_orders_response.return_value={'code':'0','data':[self.active()]}
        self.assertEqual(ReconcileStatus.FILLED,self.reconcile().status)
        self.assertEqual('ALGO',self.ledger.get_protection(self.attempt.attempt_id).algo_id)
        self.gateway.place_protection.assert_not_called()

    def test_native_missing_child_is_not_replaced_blindly(self):
        self.order.update(state='filled',accFillSz='3')
        self.assertEqual(ReconcileStatus.NEEDS_REVIEW,self.reconcile().status)
        self.gateway.place_protection.assert_not_called()
        self.assertIsNotNone(self.ledger.automation_pause_reason())

    def test_partial_native_fill_cancels_remainder_then_verifies_child(self):
        self.order.update(state='partially_filled',accFillSz='1')
        def cancel(*args):
            self.order.update(state='canceled')
            self.gateway.algo_orders_response.return_value={'code':'0','data':[self.active('1')]}
            return {'code':'0','data':[{'ordId':'ENTRY','sCode':'0'}]}
        self.gateway.cancel_entry.side_effect=cancel
        self.reconcile()
        self.gateway.cancel_entry.assert_called_once()
        self.gateway.place_protection.assert_not_called()
        self.assertEqual('1',self.ledger.get_protection(self.attempt.attempt_id).covered_size)

    def test_wrong_price_or_size_child_never_counts_as_protection(self):
        for field,value in [('sz','2'),('slTriggerPx','63000'),('tpTriggerPxType','last'),('side','buy'),('state','canceled')]:
            with self.subTest(field=field):
                item=self.active();item[field]=value
                self.assertFalse(algo_matches(item,self.attempt,self.expected,Decimal('3')))

    def test_intent_survives_restart(self):
        reopened=Ledger(self.ledger.path)
        self.assertEqual(self.expected,reopened.get_attachment_intent(self.attempt.attempt_id))

    def test_legacy_bare_live_order_is_also_blocked(self):
        with self.ledger._connection() as c:
            c.execute("DELETE FROM audit_events WHERE event_type='NATIVE_TPSL_INTENT'")
        self.order['attachAlgoOrds']=[]
        self.assertEqual(ReconcileStatus.NEEDS_REVIEW,self.reconcile().status)

    def test_unknown_partial_cancel_never_creates_duplicate(self):
        self.order.update(state='partially_filled',accFillSz='1')
        self.gateway.cancel_entry.side_effect=TimeoutError()
        self.assertEqual(ReconcileStatus.NEEDS_REVIEW,self.reconcile().status)
        self.gateway.place_protection.assert_not_called()

    def test_native_activation_delay_recovers_on_third_read(self):
        self.order.update(state='filled',accFillSz='3')
        empty={'code':'0','data':[]}
        active={'code':'0','data':[self.active()]}
        self.gateway.algo_orders_response.side_effect=[empty,empty,active,active]
        self.assertEqual(ReconcileStatus.FILLED,self.reconcile().status)
        self.assertIsNone(self.ledger.automation_pause_reason())
        self.gateway.place_protection.assert_not_called()

if __name__=='__main__':
    unittest.main()
