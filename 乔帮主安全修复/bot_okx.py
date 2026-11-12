#!/usr/bin/env python3
"""
bot_okx.py — 乔帮主策略 OKX 自动交易 (OKX SDK版)
"""

import json, os, sys, logging
from datetime import datetime, timezone, timedelta

LIVE_TRADING_DISABLED = True


def abort_if_live_trading_disabled():
    if LIVE_TRADING_DISABLED:
        print(
            "LIVE_TRADING_DISABLED: 乔帮主实盘自动交易安全锁已启用",
            file=sys.stderr,
        )
        raise SystemExit(78)


# 直接执行时必须在读取凭证、载入交易 SDK 或访问 OKX 前失败关闭。
if __name__ == '__main__':
    abort_if_live_trading_disabled()


tz_shanghai = timezone(timedelta(hours=8))

# ── .env 加载 ──────────────────────────────────────────
ENV_FILE = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

API_KEY = os.environ.get('OKX_API_KEY', '')
API_SECRET = os.environ.get('OKX_API_SECRET', '')
PASSPHRASE = os.environ.get('OKX_PASSPHRASE', '')
IS_DEMO = os.environ.get('OKX_IS_DEMO', 'false').lower() == 'true'
PROXY = os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY')

MARGIN_PCT = 0.01
LEVERAGE = 20
ENTRY_SPLITS = 5

BALANCE_FILE = os.path.join(os.path.dirname(__file__), 'okx_balance.json')
SIGNALS_FILE = os.path.join(os.path.dirname(__file__), 'signals.json')
STATE_FILE = os.path.join(os.path.dirname(__file__), 'okx_state.json')

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
log = logging.getLogger('bot_okx')

# ── OKX SDK ────────────────────────────────────────────
import okx.Account as Account
import okx.Trade as Trade
import okx.PublicData as PublicData

api_kwargs = {
    'flag': '0' if not IS_DEMO else '1',
    'debug': False,
}
if PROXY:
    api_kwargs['proxy'] = PROXY

account = Account.AccountAPI(API_KEY, API_SECRET, PASSPHRASE, **api_kwargs)
trade = Trade.TradeAPI(API_KEY, API_SECRET, PASSPHRASE, **api_kwargs)

def get_balance():
    try:
        resp = account.get_account_balance()
        for d in resp.get('data', []):
            for det in d.get('details', []):
                if det['ccy'] == 'USDT':
                    return float(det.get('availBal', 0))
    except Exception as e:
        log.error(f'获取余额失败: {e}')
    return -1

def load_balance():
    if os.path.exists(BALANCE_FILE):
        with open(BALANCE_FILE) as f:
            return json.load(f).get('balance', 0.0)
    return 0.0

def save_balance(bal):
    with open(BALANCE_FILE, 'w') as f:
        json.dump({'balance': bal, 'updated_at': datetime.now(tz_shanghai).isoformat()}, f, indent=2)

def set_leverage(symbol):
    try:
        resp = account.set_leverage(instId=symbol, lever=str(LEVERAGE), mgnMode='isolated')
        log.info(f'🔧 杠杆 {LEVERAGE}x: {resp.get("msg")}')
    except Exception as e:
        log.error(f'设置杠杆失败: {e}')

def cancel_all_orders(symbol):
    try:
        trade.cancel_order(instId=symbol)
        trade.cancel_advance_algos(instId=symbol)
        algos = trade.get_order_algos(instId=symbol, algoOrdType='conditional')
        for algo in algos.get('data', []):
            trade.cancel_algos([{'algoId': algo['algoId'], 'instId': symbol}])
        log.info(f'撤销 {symbol} 所有订单')
    except Exception as e:
        log.error(f'撤单失败: {e}')

def place_limit_orders(symbol, direction, entry_low, entry_high, total_qty):
    side = 'buy' if direction == 'long' else 'sell'
    pos_side = 'long' if direction == 'long' else 'short'
    step = (entry_high - entry_low) / (ENTRY_SPLITS - 1) if ENTRY_SPLITS > 1 else 0
    per_qty = str(max(1, int(float(total_qty) / ENTRY_SPLITS)))
    for i in range(ENTRY_SPLITS):
        px = str(round(entry_low + step * i, 1)) if entry_low + step * i > 1 else str(round(entry_low + step * i, 4))
        try:
            trade.place_order(instId=symbol, tdMode='isolated', side=side, ordType='limit', sz=per_qty, px=px, posSide=pos_side)
            log.info(f'📝 #{i+1}/{ENTRY_SPLITS}: {side} {per_qty}张 @ {px}')
        except Exception as e:
            log.error(f'挂单失败: {e}')

def place_stop_orders(symbol, direction, stop_price, target_low, target_high, qty):
    pos_side = 'long' if direction == 'long' else 'short'
    stop_side = 'short' if direction == 'long' else 'long'
    try:
        trade.place_algo_order(instId=symbol, tdMode='isolated', side=stop_side, ordType='conditional',
                               sz=qty, slTriggerPx=str(stop_price), slOrdPx='-1', posSide=pos_side)
        log.info(f'🛑 止损 @ {stop_price}')
    except Exception as e:
        log.error(f'止损失败: {e}')
    try:
        trade.place_algo_order(instId=symbol, tdMode='isolated', side=stop_side, ordType='conditional',
                               sz=qty, tpTriggerPx=str(target_low), tpOrdPx='-1', posSide=pos_side)
        log.info(f'🎯 止盈 @ {target_low}')
    except Exception as e:
        log.error(f'止盈失败: {e}')

def verify_protection(symbol, qty):
    """P0-2: 验证止损止盈订单均已挂上"""
    try:
        algos = trade.get_order_algos(instId=symbol, algoOrdType='conditional')
        live = algos.get('data', [])
        sl_ok = any(a.get('slTriggerPx') for a in live if a.get('sz') == qty)
        tp_ok = any(a.get('tpTriggerPx') for a in live if a.get('sz') == qty)
        if sl_ok and tp_ok:
            log.info('✅ 止损+止盈均已确认')
            return True
        log.warning(f'⚠️ 保护验证: 止损={sl_ok} 止盈={tp_ok}')
        return False
    except Exception as e:
        log.error(f'验证保护订单失败: {e}')
        return False

def _emergency_close(symbol, side, qty):
    """P0-2: 市价紧急平仓"""
    try:
        pos_side = 'long' if side == 'long' else 'short'
        trade.place_order(instId=symbol, tdMode='isolated', side=side,
                          ordType='market', sz=qty, posSide=pos_side)
        log.warning(f'🚨 紧急平仓: {symbol} {side} {qty}张 市价')
    except Exception as e:
        log.error(f'紧急平仓失败!! 需人工处理: {e}')

def process_signal(signal):
    """处理单条信号。P0修复：下单前标SUBMITTING，保护失败则紧急平仓"""
    symbol = signal['symbol']
    direction = signal['direction']
    entry_low = signal['entry_low']
    entry_high = signal['entry_high']
    stop_price = signal['stop']
    target_low = signal['target_low']
    target_high = signal['target_high']

    log.info(f'\n{"="*50}\n🚀 {symbol} {direction}\n  入场:{entry_low}-{entry_high} 止损:{stop_price} 止盈:{target_low}-{target_high}\n{"="*50}')

    # P0-1: 下单前立即标记 SUBMITTING，防止并发重复
    if not _set_signal_status(signal, 'SUBMITTING'):
        log.warning(f'⚠️ 信号已被其他进程处理或状态不是pending，跳过')
        return

    set_leverage(symbol)
    cancel_all_orders(symbol)

    capital = get_balance()
    if capital <= 0:
        capital = load_balance()
    if capital <= 0:
        log.error('❌ 无余额')
        _set_signal_status(signal, 'FAILED')
        return

    position_value = MARGIN_PCT * LEVERAGE * capital
    entry_mid = (entry_low + entry_high) / 2
    contract_size = 0.01 if symbol.startswith(('BTC', 'ETH')) else 1
    qty = max(1, int(position_value / (entry_mid * contract_size)))
    log.info(f'💰 资金:{capital:.0f} 仓位:{position_value:.0f} 张数:{qty}')

    # Step 1: 入场
    try:
        place_limit_orders(symbol, direction, entry_low, entry_high, str(qty))
    except Exception as e:
        log.error(f'❌ 入场失败: {e}')
        _set_signal_status(signal, 'FAILED')
        return

    # Step 2: 保护（P0-2修复：验证 + 失败则紧急平仓）
    try:
        place_stop_orders(symbol, direction, stop_price, target_low, target_high, str(qty))
    except Exception as e:
        log.error(f'❌ 保护设置失败: {e}')
        _emergency_close(symbol, 'long' if direction == 'short' else 'short', str(qty))
        _set_signal_status(signal, 'FAILED')
        return

    if not verify_protection(symbol, str(qty)):
        log.error('❌ 保护订单验证失败，触发紧急平仓')
        _emergency_close(symbol, 'long' if direction == 'short' else 'short', str(qty))
        _set_signal_status(signal, 'FAILED')
        return

    # 全部成功
    mark_signal_processed(signal, qty)

def _set_signal_status(signal, status: str, **extra):
    """原子更新信号状态（P0修复：三步标记杜绝重复下单）
    允许从 pending→SUBMITTING→DONE/FAILED 的合法转换"""
    if not os.path.exists(SIGNALS_FILE):
        return False
    with open(SIGNALS_FILE, 'r', encoding='utf-8') as f:
        all_signals = json.load(f)
    found = False
    for s in all_signals:
        current = s.get('status', 'pending')
        # 合法转换: pending→SUBMITTING  或  SUBMITTING→DONE/FAILED
        valid = (current == 'pending' and status == 'SUBMITTING') or \
                (current == 'SUBMITTING' and status in ('DONE', 'FAILED'))
        if s.get('raw_text') == signal.get('raw_text') and valid:
            s['status'] = status
            s['status_at'] = datetime.now(tz_shanghai).isoformat()
            for k, v in extra.items():
                s[k] = v
            found = True
            break
    if not found:
        return False
    with open(SIGNALS_FILE, 'w', encoding='utf-8') as f:
        json.dump(all_signals, f, ensure_ascii=False, indent=2)
    return True

def mark_signal_processed(signal, qty):
    _set_signal_status(signal, 'DONE', qty=qty)

def main():
    abort_if_live_trading_disabled()
    if not API_KEY:
        log.error('❌ 缺少 API Key')
        sys.exit(1)
    mode = "模拟盘" if IS_DEMO else "实盘"
    log.info(f'🔌 OKX {mode}')

    if not os.path.exists(SIGNALS_FILE):
        log.info('📭 暂无信号')
        return

    with open(SIGNALS_FILE, 'r', encoding='utf-8') as f:
        all_signals = json.load(f)
    pending = [s for s in all_signals if s.get('status', 'pending') == 'pending' and s.get('action') != 'hold'
               and s.get('status') != 'SUBMITTING']  # P0: SUBMITTING跳过
    if not pending:
        log.info('📭 无待处理信号')
        return

    log.info(f'📋 {len(pending)} 条待处理')
    for sig in pending:
        try:
            process_signal(sig)
        except Exception as e:
            log.error(f'❌ {e}')
            _set_signal_status(sig, 'FAILED')  # P0: 异常时标记失败,防止永久卡SUBMITTING

    bal = get_balance()
    if bal > 0:
        save_balance(bal)

if __name__ == '__main__':
    main()
