"""Offline scenario against deployed source. Network and production ledger are unused."""
import copy
import json
import socket
import sys
import tempfile
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

PROJECT=Path('/Users/USER/乔帮主项目_移交包/qiaobangzhu')
ROOT=Path(__file__).resolve().parent
CANDIDATE = '--candidate' in sys.argv
sys.path.insert(0,str(ROOT/'candidate' if CANDIDATE else PROJECT))
from src_v2.parser import analyze_message
from src_v2.ledger import Ledger
from src_v2.executor import Executor
from src_v2.reconciler import Reconciler

class SimulatedExchange:
    def __init__(self):
        self.payload=None
        self.state='live'
        self.child_active=False
        self.closed=False
        self.omit_stop=False
    def positions(self,symbol): return []
    def pending_orders(self,symbol): return []
    def available_usdt(self): return Decimal('1000')
    def current_price(self,symbol): return Decimal('99000')
    def instrument(self,symbol):
        return dict(tickSz='0.1',lotSz='0.01',minSz='0.01',ctVal='0.01',ctMult='1')
    def ensure_leverage(self,*args): return {'code':'0','data':[{'sCode':'0'}]}
    def place_order(self,payload):
        self.payload=copy.deepcopy(payload)
        return {'code':'0','data':[{'ordId':'SIMULATED-ENTRY-NOT-OKX','clOrdId':payload['clOrdId'],'sCode':'0'}]}
    def order(self,symbol,**kwargs):
        order={**copy.deepcopy(self.payload),'ordId':'SIMULATED-ENTRY-NOT-OKX','state':self.state,'accFillSz':self.payload['sz'] if self.state=='filled' else '0'}
        if self.omit_stop: order['attachAlgoOrds'][0].pop('slTriggerPx',None)
        return {'code':'0','data':[order]}
    def child(self):
        terms=self.payload['attachAlgoOrds'][0]
        return {**terms,'algoId':'SIMULATED-PROTECTION-NOT-OKX','algoClOrdId':terms['attachAlgoClOrdId'],'instId':self.payload['instId'],'posSide':'short','side':'buy','sz':self.payload['sz'],'ordType':'oco','state':'live'}
    def algo_orders_response(self,symbol):
        return {'code':'0','data':[self.child()] if self.child_active and not self.closed else []}
    def algo_history_response(self,symbol,**kwargs):
        return {'code':'0','data':[{**self.child(),'state':'effective'}] if self.closed else []}
    def positions_response(self,symbol):
        return {'code':'0','data':[{'instId':symbol,'posSide':'short','pos':'0' if self.closed else self.payload['sz']}]}
    def place_protection(self,*args): raise AssertionError('Must not duplicate native protection')
    def cancel_entry(self,*args): raise AssertionError('No cancellation expected in this full-fill scenario')


def main():
    raw='btc 100000空单。105000止损，98000止盈'
    canonical='BTC 100000做空，止损105000，止盈98000'
    report={'mode':'OFFLINE_SIMULATION_NOT_OKX_DEMO','network':'DISABLED','raw_input':raw,'normalized_input':canonical,'assumptions':{'simulated_current_price':'99000','simulated_available_usdt':'1000','instrument_spec':'fixture only'},'live_orders':0,'source':'UNDEPLOYED_PARSER_CANDIDATE' if CANDIDATE else 'DEPLOYED_CODE'}
    with patch.object(socket.socket,'connect',side_effect=AssertionError('NETWORK FORBIDDEN')),patch.object(socket,'create_connection',side_effect=AssertionError('NETWORK FORBIDDEN')):
        d=analyze_message(raw)
        report['raw_parser']={'kind':d.kind.value,'questions':d.questions}
        decision=analyze_message(raw if CANDIDATE else canonical)
        assert decision.kind.value=='READY' and len(decision.signals)==1
        signal=decision.signals[0]
        report['normalized_signal']=asdict(signal)
        with tempfile.TemporaryDirectory(prefix='hermes-offline-btc-') as tmp:
            ledger=Ledger(Path(tmp)/'simulation.sqlite3')
            ledger.record_strategy(strategy_id='offline-btc100000',symbol=signal.symbol,source_chat_id=-1,source_sender_id=1,source_message_id=1,raw_text=canonical,directions=('short',),signals=(signal,))
            gateway=SimulatedExchange()
            outcome=Executor(ledger=ledger,gateway=gateway).execute_strategy('offline-btc100000',(signal,))
            assert outcome.status.value=='SUBMITTED'
            payload=gateway.payload
            assert payload['side']=='sell' and payload['posSide']=='short' and payload['ordType']=='limit' and payload['px']=='100000'
            attached=payload['attachAlgoOrds'][0]
            assert attached['slTriggerPx']=='105000' and attached['tpTriggerPx']=='98000'
            assert attached['slOrdPx']==attached['tpOrdPx']=='-1'
            report['generated_order_request']=payload
            attempt=ledger.list_attempts('offline-btc100000')[0]
            report['simulated_entry_state']=ledger.get_attempt(attempt.attempt_id).state
            reopened=Ledger(ledger.path)
            restart=Reconciler(ledger=reopened,gateway=gateway).reconcile(attempt.attempt_id)
            assert restart.status.value=='LIVE'
            report['restart_reconciliation']=restart.status.value
            gateway.state='filled';gateway.child_active=True
            filled=Reconciler(ledger=reopened,gateway=gateway).reconcile(attempt.attempt_id)
            assert filled.status.value=='FILLED'
            report['simulated_filled_protection']=asdict(reopened.get_protection(attempt.attempt_id))
            gateway.closed=True
            closed=Reconciler(ledger=reopened,gateway=gateway).reconcile(attempt.attempt_id)
            assert closed.status.value=='CLOSED'
            report['simulated_exit_reconciliation']=closed.status.value
        with tempfile.TemporaryDirectory(prefix='hermes-offline-negative-') as tmp:
            ledger=Ledger(Path(tmp)/'simulation.sqlite3')
            ledger.record_strategy(strategy_id='offline-missing-stop',symbol=signal.symbol,source_chat_id=-1,source_sender_id=1,source_message_id=2,raw_text=canonical,directions=('short',),signals=(signal,))
            gateway=SimulatedExchange();gateway.omit_stop=True
            rejected=Executor(ledger=ledger,gateway=gateway).execute_strategy('offline-missing-stop',(signal,))
            assert rejected.status.value!='SUBMITTED' and ledger.automation_pause_reason()
            report['missing_stop_negative_test']={'outcome':rejected.status.value,'paused':True}
    report['local_scenario_checks']='PASS'
    (ROOT/('simulation-result-candidate.json' if CANDIDATE else 'simulation-result.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
