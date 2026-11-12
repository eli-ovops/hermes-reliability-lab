"""Offline replay of captured Sept 11 messages. No SDK, credentials or network."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import socket
import tempfile
from unittest.mock import patch

from src_v2.attached_protection import attachment_matches
from src_v2.ingest import Intake
from src_v2.ledger import Ledger
from src_v2.parser import TradeSignal
from src_v2.runtime import RuntimeMode, StrategyRuntime
from src_v2.service import TradingService
from src_v2.tg_adapter import TelegramAdapter
from src_v2.tests.test_runtime import RuntimeGateway

ROOT = Path(__file__).resolve().parent


class ReplayGateway(RuntimeGateway):
    def __init__(self, fixture):
        super().__init__()
        self.fixture = fixture
        self.read_failures = {'BTC-USDT-SWAP':3, 'ETH-USDT-SWAP':3}

    def positions(self, symbol):
        if self.read_failures.get(symbol, 0):
            self.read_failures[symbol] -= 1
            raise TimeoutError('simulated slow preflight read')
        return []

    def current_price(self, symbol):
        return Decimal({'BTC-USDT-SWAP':'77000', 'ETH-USDT-SWAP':'2465', 'UNI-USDT-SWAP':'6.01'}[symbol])

    def instrument(self, symbol):
        return {**super().instrument(symbol), 'tickSz':'0.001' if symbol=='UNI-USDT-SWAP' else '0.1'}

    def place_order(self, payload):
        response = super().place_order(payload)
        old_id = response['data'][0]['ordId']
        new_id = 'SIMULATED-' + old_id
        self.orders[new_id] = self.orders.pop(old_id)
        response['data'][0]['ordId'] = new_id
        return response

    def order(self, symbol, *, ord_id=None, cl_ord_id=None):
        old = self.fixture['old_uni']
        if ord_id == old['ord_id']:
            return {'code':'0', 'data':[{'instId':symbol, 'clOrdId':old['cl_ord_id'],
                'ordId':old['ord_id'], 'state':'filled', 'accFillSz':old['filled_size']}]}
        return super().order(symbol, ord_id=ord_id, cl_ord_id=cl_ord_id)

    def positions_response(self, symbol):
        return {'code':'0', 'data':[]}

    def algo_orders_response(self, symbol):
        return {'code':'0', 'data':[]}

    def algo_history_response(self, symbol, *, algo_id):
        return {'code':'0', 'data':self.fixture['algo_history']}


def main():
    fixture = json.loads((ROOT/'today-fixture.json').read_text())
    report = {'mode':'OFFLINE_SIMULATION', 'network_requests':0,
        'synthetic_inputs':['available USDT=1000','test contract lot/face values','current BTC=77000 ETH=2465 UNI=6.01',
                            'three injected preflight read timeouts each for BTC/ETH'], 'orders':[]}
    with tempfile.TemporaryDirectory(prefix='hermes-replay-') as temp, patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden in replay')):
        ledger = Ledger(Path(temp)/'ledger.sqlite3')
        old = fixture['old_uni']
        signal = TradeSignal(symbol=old['symbol'], direction='long', entry_low=float(old['entry_low']),
            entry_high=float(old['entry_high']), entry_price=float(old['entry_price']), stop=float(old['stop_price']), target=float(old['target_price']))
        ledger.record_strategy(strategy_id=old['strategy_id'], symbol=old['symbol'],
            source_chat_id=old['source_chat_id'], source_sender_id=old['source_sender_id'],
            source_message_id=old['source_message_id'], raw_text='captured old UNI execution fixture',
            directions=('long',), signals=(signal,))
        # Seed the isolated fixture with the captured identity; never open the
        # production ledger in this replay executable.
        with ledger._connection() as c:
            c.execute('UPDATE direction_attempts SET cl_ord_id=? WHERE attempt_id=?', (old['cl_ord_id'],old['attempt_id']))
            c.commit()
        ledger.transition_attempt(old['attempt_id'], expected_states=('READY',), new_state='FILLED',
            ord_id=old['ord_id'], requested_size=old['requested_size'], filled_size=old['filled_size'])
        protection = fixture['protection']
        ledger.record_protection(old['attempt_id'], algo_id=protection['algo_id'], covered_size=protection['covered_size'], state='ACTIVE')
        ledger.record_attachment_intent(old['attempt_id'], fixture['intent']['payload'])
        gateway = ReplayGateway(fixture)
        service = TradingService(ledger=ledger, gateway=gateway, lock_path=Path(temp)/'lock', require_source_attestation=True)
        runtime = StrategyRuntime(adapter=TelegramAdapter(intake=Intake(ledger=ledger,
            allowed_chat_ids={e['chat_id'] for e in fixture['events']}, allowed_sender_ids={e['sender_id'] for e in fixture['events']},
            dispatch_enabled=True)), mode=RuntimeMode.LIVE, service=service, defer_execution=True)
        now = datetime.now(timezone.utc)
        events = [{k:e[k] for k in ('chat_id','sender_id','message_id')} | {'sender_username':'captured',
            'message_date':now, 'text':e['raw_text']} for e in fixture['events']]
        received = [runtime.on_new_message(**event) for event in events]
        for seconds in (0, 180, 400, 700):
            service = TradingService(ledger=Ledger(ledger.path), gateway=gateway, lock_path=Path(temp)/'lock', require_source_attestation=True)
            service.dispatch_pending(now=now+timedelta(seconds=seconds))
        for event in events:
            runtime.on_new_message(**event)
        service.dispatch_pending(now=now+timedelta(seconds=1000))
        report['old_uni_state'] = ledger.get_attempt(old['attempt_id']).state
        assert report['old_uni_state'] == 'CLOSED'
        for result in received:
            job = ledger.get_execution_job(result.strategy_id)
            assert job['state'] == 'DONE', job
            for attempt in ledger.list_attempts(result.strategy_id):
                payload = gateway.orders[attempt.ord_id]
                assert attachment_matches({'attachAlgoOrds':payload['attachAlgoOrds']}, ledger.get_attachment_intent(attempt.attempt_id))
                report['orders'].append({'symbol':attempt.symbol, 'direction':attempt.direction, 'state':attempt.state,
                    'simulated_order_id':attempt.ord_id, 'attempts':job['attempts'],
                    'tp':payload['attachAlgoOrds'][0]['tpTriggerPx'], 'sl':payload['attachAlgoOrds'][0]['slTriggerPx']})
        assert len(gateway.placed) == len(report['orders']) == 5
        report['passed'] = True
    (ROOT/'today-replay-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
