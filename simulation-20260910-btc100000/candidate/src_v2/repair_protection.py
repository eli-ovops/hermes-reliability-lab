"""User-operated cancellation of exact owned unfilled bare incident orders.
No new entry or position-closing orders; never invoked by the normal runtime.
"""
from decimal import Decimal

def cancel_unprotected(ledger, gateway, attempt_id, *, apply=False):
    """Incident containment; cancel only an exact owned, unfilled, bare entry."""
    attempt=ledger.get_attempt(attempt_id)
    if attempt is None or not attempt.ord_id:
        return {'status':'BLOCKED','reason':'订单归属缺失'}
    result={'attempt_id':attempt_id,'symbol':attempt.symbol,'direction':attempt.direction,'ordId':attempt.ord_id}
    def read():
        response=gateway.order(attempt.symbol,ord_id=attempt.ord_id,cl_ord_id=attempt.cl_ord_id)
        if not isinstance(response,dict) or response.get('code')!='0' or not isinstance(response.get('data'),list):
            raise ValueError('Order read failed')
        rows=[x for x in response['data'] if x.get('instId')==attempt.symbol and x.get('ordId')==attempt.ord_id and x.get('clOrdId')==attempt.cl_ord_id and x.get('posSide')==attempt.direction and x.get('tdMode')=='isolated' and x.get('side')==('buy' if attempt.direction=='long' else 'sell')]
        if len(rows)!=1:
            raise ValueError('Order identity mismatch')
        return rows[0]
    try:
        order=read()
        if Decimal(str(order.get('accFillSz'))) != 0:
            return {**result,'status':'BLOCKED','reason':'订单已有成交，需要核对仓位；未撤单'}
        if order.get('state') in {'canceled','mmp_canceled'}:
            return {**result,'status':'CANCELED_NO_FILL'}
        if order.get('state')!='live':
            return {**result,'status':'BLOCKED','reason':'订单状态不允许按裸挂单撤销'}
        if order.get('attachAlgoOrds') or order.get('tpTriggerPx') or order.get('slTriggerPx'):
            return {**result,'status':'PRESERVED_PROTECTION_PRESENT','reason':'发现保护配置，保留订单待核验'}
        algos=gateway.algo_orders_response(attempt.symbol)
        if not isinstance(algos,dict) or algos.get('code')!='0' or not isinstance(algos.get('data'),list):
            return {**result,'status':'BLOCKED','reason':'保护查询失败，未撤单'}
        if any(x.get('instId')==attempt.symbol and x.get('posSide')==attempt.direction for x in algos['data']):
            return {**result,'status':'PRESERVED_PROTECTION_PRESENT','reason':'发现独立保护，保留订单待核验'}
        if not apply:
            return {**result,'status':'WOULD_CANCEL_UNPROTECTED'}
        try:
            gateway.cancel_entry(attempt.symbol,attempt.ord_id)
        except Exception:
            pass
        for _ in range(3):
            try:
                after=read()
                if Decimal(str(after.get('accFillSz'))) != 0:
                    return {**result,'status':'BLOCKED','reason':'撤单期间发生成交，必须检查持仓及保护；未平仓'}
                if after.get('state') in {'canceled','mmp_canceled'}:
                    return {**result,'status':'CANCELED_NO_FILL'}
            except Exception:
                pass
        return {**result,'status':'BLOCKED','reason':'撤单未取得终态确认；未重复提交'}
    except Exception as exc:
        return {**result,'status':'BLOCKED','reason':'核对失败 '+type(exc).__name__}
