"""Exchange audit. Transport rejects every non-GET request; no runtime is started."""
import json
import hmac
import os
import plistlib
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
PROJECT = Path('/Users/USER/乔帮主项目_移交包/qiaobangzhu')

def main():
    sys.path.insert(0, str(PROJECT))
    from src_v2.live_bootstrap import _secure_read, _parse_simple_env
    from src_v2.sdk_factory import build_gateway, verify_account_config
    import httpx
    cfg = plistlib.loads(Path('/Users/USER/Library/LaunchAgents/com.bangzhu.v2.plist').read_bytes())
    env = cfg['EnvironmentVariables']
    secrets = _parse_simple_env(_secure_read(env['V2_OKX_ENV_FILE'], label='configured OKX credentials'))
    original_send = httpx.Client.send
    def get_only(self, request, *args, **kwargs):
        if request.method != 'GET':
            raise RuntimeError('Read-only audit forbids non-GET')
        return original_send(self, request, *args, **kwargs)
    report = {'at': datetime.now(timezone.utc).isoformat(), 'mode': 'GET_ONLY', 'orders': [], 'positions': [], 'algos': [], 'errors': []}
    with patch.object(httpx.Client, 'send', get_only):
        gateway = build_gateway(api_key=secrets['OKX_API_KEY'], api_secret=secrets['OKX_API_SECRET'], passphrase=secrets['OKX_PASSPHRASE'], demo=False, proxy=env.get('V2_OKX_PROXY'))
        summary = verify_account_config(gateway.account)
        if not hmac.compare_digest(summary['account_fingerprint'], env['V2_OKX_ACCOUNT_FINGERPRINT']):
            raise RuntimeError('Account binding mismatch')
        report['account_verified'] = True
        with sqlite3.connect(Path(env['V2_LEDGER_PATH']).as_uri()+'?mode=ro', uri=True) as c:
            c.row_factory = sqlite3.Row
            c.execute('PRAGMA query_only=ON')
            targets = json.loads((ROOT/'incident-targets.json').read_text())
            ids = [t['ordId'] for t in targets]
            placeholders = ','.join('?' for _ in ids)
            rows = [dict(r) for r in c.execute("SELECT attempt_id,symbol,direction,cl_ord_id,ord_id,state,stop_price,target_price FROM direction_attempts WHERE (ord_id IS NOT NULL AND state NOT IN ('CLOSED','CANCELED','MISSING_CONFIRMED')) OR ord_id IN ("+placeholders+")", ids)]
        for row in rows:
            try:
                response = gateway.order(row['symbol'], ord_id=row['ord_id'])
                if response.get('code') != '0':
                    raise RuntimeError('Order query failed')
                matches = [d for d in response.get('data', []) if d.get('ordId') == row['ord_id'] and d.get('clOrdId') == row['cl_ord_id'] and d.get('instId') == row['symbol']]
                if len(matches) != 1:
                    raise RuntimeError('Order identity mismatch')
                fields = ('instId','ordId','clOrdId','state','posSide','side','tdMode','sz','accFillSz','px','attachAlgoOrds','tpTriggerPx','slTriggerPx','tpOrdPx','slOrdPx','tpTriggerPxType','slTriggerPxType')
                report['orders'].append({'ledger': row, 'exchange': {k:matches[0].get(k) for k in fields}})
            except Exception as exc:
                report['errors'].append({'symbol':row['symbol'], 'ordId':row['ord_id'], 'error':type(exc).__name__})
        for symbol in sorted({r['symbol'] for r in rows}):
            for key, call in [('positions',lambda:gateway.positions_response(symbol)), ('algos',lambda:gateway.algo_orders_response(symbol))]:
                try:
                    response = call()
                    if response.get('code') != '0' or not isinstance(response.get('data'),list):
                        raise RuntimeError('Query failed')
                    fields = ('instId','posSide','pos','mgnMode','liqPx','algoId','algoClOrdId','ordType','state','sz','tpTriggerPx','slTriggerPx','tpOrdPx','slOrdPx','tpTriggerPxType','slTriggerPxType','side')
                    report[key].extend({k:d[k] for k in fields if k in d} for d in response['data'])
                except Exception as exc:
                    report['errors'].append({'symbol':symbol,'query':key,'error':type(exc).__name__})
    path = ROOT/('exchange-audit-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.json')
    with path.open('x') as f:
        os.chmod(path,0o600)
        json.dump(report,f,ensure_ascii=False,indent=2)
    print(json.dumps(report, ensure_ascii=False))
    print('Report:',path)

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Read-only audit blocked:', type(exc).__name__)
        raise SystemExit(1)
