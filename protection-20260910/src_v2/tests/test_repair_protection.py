import unittest
from src_v2.tests import test_attached_protection as native
from src_v2.repair_protection import cancel_unprotected

class IncidentContainmentTests(unittest.TestCase):
    def setUp(self):
        native.NativeProtectionTests.setUp(self)
        self.order.update(attachAlgoOrds=[],posSide='long',side='buy',tdMode='isolated',px='65000')
        def cancel(*args):
            self.order['state']='canceled'
            return {'code':'0','data':[{'ordId':'ENTRY','sCode':'0'}]}
        self.gateway.cancel_entry.side_effect=cancel

    def run_cancel(self,apply=True):
        return cancel_unprotected(self.ledger,self.gateway,self.attempt.attempt_id,apply=apply)

    def test_exact_unfilled_bare_order_is_confirmed_canceled(self):
        self.assertEqual('CANCELED_NO_FILL',self.run_cancel()['status'])
        self.gateway.cancel_entry.assert_called_once_with('BTC-USDT-SWAP','ENTRY')
        self.gateway.place_order.assert_not_called()
        self.gateway.place_protection.assert_not_called()

    def test_acknowledgment_alone_does_not_count_as_canceled(self):
        self.gateway.cancel_entry.side_effect=None
        self.assertEqual('BLOCKED',self.run_cancel()['status'])
        self.gateway.cancel_entry.assert_called_once()
        self.assertEqual(4,self.gateway.order.call_count)

    def test_timeout_after_cancel_is_recovered_without_duplicate(self):
        def lose(*args):
            self.order['state']='canceled'
            raise TimeoutError()
        self.gateway.cancel_entry.side_effect=lose
        self.assertEqual('CANCELED_NO_FILL',self.run_cancel()['status'])
        self.gateway.cancel_entry.assert_called_once()

    def test_fill_during_cancel_is_explicitly_unresolved(self):
        def fill(*args):
            self.order.update(state='canceled',accFillSz='1')
        self.gateway.cancel_entry.side_effect=fill
        self.assertEqual('BLOCKED',self.run_cancel()['status'])
        self.gateway.place_order.assert_not_called()

    def test_partial_fill_before_cancel_is_preserved(self):
        self.order.update(state='partially_filled',accFillSz='1')
        self.assertEqual('BLOCKED',self.run_cancel()['status'])
        self.gateway.cancel_entry.assert_not_called()

    def test_wrong_identity_is_preserved(self):
        self.order['clOrdId']='MANUAL'
        self.assertEqual('BLOCKED',self.run_cancel()['status'])
        self.gateway.cancel_entry.assert_not_called()

    def test_existing_attached_protection_is_preserved(self):
        self.order['attachAlgoOrds']=[self.expected]
        self.assertEqual('PRESERVED_PROTECTION_PRESENT',self.run_cancel()['status'])
        self.gateway.cancel_entry.assert_not_called()

    def test_existing_standalone_protection_is_preserved(self):
        self.gateway.algo_orders_response.return_value={'code':'0','data':[{'instId':self.signal.symbol,'posSide':'long'}]}
        self.assertEqual('PRESERVED_PROTECTION_PRESENT',self.run_cancel()['status'])
        self.gateway.cancel_entry.assert_not_called()

    def test_already_canceled_is_idempotent(self):
        self.order['state']='canceled'
        self.assertEqual('CANCELED_NO_FILL',self.run_cancel()['status'])
        self.gateway.cancel_entry.assert_not_called()

    def test_preview_never_cancels(self):
        self.assertEqual('WOULD_CANCEL_UNPROTECTED',self.run_cancel(False)['status'])
        self.gateway.cancel_entry.assert_not_called()
