"""Native attached TP/SL intent and strict exchange verification."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

INCIDENT = '保护事故：'

def decimal_text(value):
    value = Decimal(str(value))
    if not value.is_finite():
        raise ValueError('Non-finite protection price')
    return format(value.normalize(), 'f')

def attachment(client_id, stop, target):
    if not client_id.isalnum() or len(client_id) > 31:
        raise ValueError('Invalid protection client ID')
    if Decimal(str(stop)) <= 0 or Decimal(str(target)) <= 0:
        raise ValueError('Invalid protection prices')
    return dict(attachAlgoClOrdId=client_id+'P',
                tpTriggerPx=decimal_text(target), tpOrdPx='-1', tpTriggerPxType='mark',
                slTriggerPx=decimal_text(stop), slOrdPx='-1', slTriggerPxType='mark')

def expected_protection(ledger, gateway, attempt):
    saved = ledger.get_attachment_intent(attempt.attempt_id)
    if saved:
        return saved
    signal = ledger.get_attempt_signal(attempt.attempt_id)
    if signal is None:
        raise ValueError('Missing strategy prices')
    tick = Decimal(str(gateway.instrument(attempt.symbol)['tickSz']))
    if not tick.is_finite() or tick <= 0:
        raise ValueError('Invalid tick')
    stop = (Decimal(str(signal.stop))/tick).to_integral_value(rounding=ROUND_HALF_UP)*tick
    target = (Decimal(str(signal.target))/tick).to_integral_value(rounding=ROUND_HALF_UP)*tick
    entry = Decimal(str(signal.entry_price))
    if not (stop < entry < target if attempt.direction == 'long' else target < entry < stop):
        raise ValueError('Invalid rounded protection direction')
    return attachment(attempt.cl_ord_id, stop, target)

def prices_match(actual, expected):
    try:
        return all(Decimal(str(actual.get(k))).is_finite() and
                   Decimal(str(actual.get(k))) == Decimal(str(expected[k]))
                   for k in ('tpTriggerPx','slTriggerPx','tpOrdPx','slOrdPx')) and all(
                       actual.get(k) == expected[k] for k in ('tpTriggerPxType','slTriggerPxType'))
    except (InvalidOperation, TypeError, ValueError, KeyError):
        return False

def attachment_matches(order, expected):
    items = order.get('attachAlgoOrds')
    return (isinstance(items, list) and len(items) == 1 and
            isinstance(items[0], dict) and
            items[0].get('attachAlgoClOrdId') == expected['attachAlgoClOrdId'] and
            items[0].get('failCode') in (None, '', '0') and
            not items[0].get('failReason') and prices_match(items[0], expected))

def algo_matches(item, attempt, expected, filled):
    try:
        size_ok = Decimal(str(item.get('sz'))) == filled and filled > 0
    except (InvalidOperation, ValueError, TypeError):
        return False
    return (item.get('instId') == attempt.symbol and item.get('posSide') == attempt.direction and
            item.get('side') == ('sell' if attempt.direction == 'long' else 'buy') and
            item.get('ordType') == 'oco' and item.get('state') == 'live' and
            item.get('algoClOrdId') == expected['attachAlgoClOrdId'] and
            bool(item.get('algoId')) and size_ok and prices_match(item, expected))
