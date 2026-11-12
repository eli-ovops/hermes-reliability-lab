"""User-operated deployment and cancellation of six unprotected incident orders.

--apply --cancel-unprotected cancels only the exact six owned, unfilled, bare
orders. It never creates orders, closes positions, or resumes new entries.
"""
import argparse
import hmac
import json
import os
from pathlib import Path
import plistlib
from datetime import datetime
from unittest.mock import patch
import deploy_once

ROOT=Path(__file__).resolve().parent

def remediation(ledger_path):
    import httpx
    from src_v2.ledger import Ledger
    from src_v2.sdk_factory import build_gateway, verify_account_config
    from src_v2.live_bootstrap import _secure_read, _parse_simple_env
    from src_v2.repair_protection import cancel_unprotected
    env=plistlib.loads(deploy_once.PLIST.read_bytes())['EnvironmentVariables']
    secrets=_parse_simple_env(_secure_read(env['V2_OKX_ENV_FILE'],label='configured OKX credentials'))
    ledger=Ledger(ledger_path)
    if ledger.automation_pause_reason() is None:
        raise RuntimeError('新开仓必须保持暂停')
    targets=json.loads((ROOT/'incident-targets.json').read_text())
    allowed={(t['ordId'],t['clOrdId']) for t in targets}
    original=httpx.Client.send
    def restricted_send(self,request,*args,**kwargs):
        if request.method != 'GET':
            if request.method != 'POST' or request.url.path != '/api/v5/trade/cancel-order':
                raise RuntimeError('Only incident order cancellation is permitted')
            body=json.loads(request.content)
            if body.get('ordId') not in {t[0] for t in allowed} or set(body) - {'instId','ordId','clOrdId'}:
                raise RuntimeError('Unexpected cancellation payload')
        return original(self,request,*args,**kwargs)
    results=[]
    with patch.object(httpx.Client,'send',restricted_send):
        gateway=build_gateway(api_key=secrets['OKX_API_KEY'],api_secret=secrets['OKX_API_SECRET'],passphrase=secrets['OKX_PASSPHRASE'],demo=False,proxy=env.get('V2_OKX_PROXY'))
        info=verify_account_config(gateway.account)
        if not hmac.compare_digest(info['account_fingerprint'],env['V2_OKX_ACCOUNT_FINGERPRINT']):
            raise RuntimeError('账户身份不匹配')
        for target in targets:
            attempt=ledger.get_attempt(target['attempt_id'])
            if attempt is None or (attempt.ord_id,attempt.cl_ord_id) != (target['ordId'],target['clOrdId']):
                result={'ordId':target['ordId'],'status':'BLOCKED','reason':'事故订单账本归属已变化'}
            else:
                result=cancel_unprotected(ledger,gateway,attempt.attempt_id,apply=True)
            results.append(result)
            print(json.dumps(result,ensure_ascii=False),flush=True)
    path=ROOT/('protection-receipts-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.json')
    with path.open('x') as f:
        os.chmod(path,0o600)
        json.dump(results,f,ensure_ascii=False,indent=2)
    print('逐单回执：',path,flush=True)
    return results

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--cancel-unprotected',action='store_true',help='取消清单内仍未成交且无保护的旧挂单；不平仓、不下新单')
    args=parser.parse_args()
    if args.apply and not args.cancel_unprotected:
        parser.error('--apply 必须同时明确指定 --cancel-unprotected')
    source,ledger,changed,already=deploy_once.check_config()
    if not args.apply:
        print('本地预检通过。--apply --cancel-unprotected 将部署修复并撤掉清单内仍未成交且无保护的旧挂单；新开仓保持暂停。')
        return
    results=[]
    def run_remediation(path):
        # Keep the new-entry switch paused even if an API request fails.
        try:
            results.extend(remediation(path))
        except Exception as exc:
            results.append({'status':'BLOCKED','reason':type(exc).__name__})
            print('事故处置未完成：'+type(exc).__name__,flush=True)
    deploy_once.apply_deployment(source,ledger,changed,before_restart=run_remediation)
    completed=len(results)==6 and all(r['status'] in {'CANCELED_NO_FILL'} for r in results)
    if completed:
        print('清单内 6 笔无保护旧挂单均已确认撤销且未成交。新开仓仍暂停；本次未提交新入场单。')
    else:
        print('事故尚未闭环：仍有 BLOCKED。请核对这些订单及持仓，新开仓保持暂停。')
        raise SystemExit(2)

if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        print('未完成：'+type(exc).__name__+'；请保留现场与备份，新开仓保持暂停。')
        raise SystemExit(1)
