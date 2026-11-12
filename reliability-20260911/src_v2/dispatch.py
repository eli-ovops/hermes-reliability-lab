"""Durable new-signal dispatch. Called only while the service execution lock is held."""
from datetime import datetime, timedelta, timezone

from src_v2.executor import ExecutionStatus


RETRY_DELAYS = (30, 60, 120)  # initial attempt plus three durable retries
OUTAGE_RETRY_DELAY = 300  # continue read-failure recovery without a tight loop


def dispatch_pending(service, *, now=None):
    ledger = service.ledger
    fixed_now = now
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if ledger.automation_pause_reason():
        return ()
    outcomes = []
    for job in ledger.due_execution_jobs(now.isoformat()):
        if ledger.automation_pause_reason():
            break
        sid = job['strategy_id']
        strategy = ledger.get_strategy(sid)
        attempts = ledger.list_attempts(sid)
        states = {a.state for a in attempts}

        def finish(state, reason='', delay=0, started=False):
            finished_at = now if fixed_now is not None else datetime.now(timezone.utc)
            notification = None
            if state in {'DONE','BLOCKED','EXPIRED','SUPERSEDED','RECONCILING'}:
                receipts = '; '.join(f'{a.symbol} {a.direction}: {a.state}, ordId={a.ord_id or "NONE"}' for a in ledger.list_attempts(sid))
                notification = dict(dedupe_key=f'dispatch:{sid}:{state}', kind='EXECUTION' if state=='DONE' else 'REVIEW_REQUIRED',
                    message=f'策略 {sid}: {state}; {reason}; {receipts}')
            elif state == 'RETRY' and job['attempts'] >= len(RETRY_DELAYS):
                notification = dict(dedupe_key=f'dispatch:{sid}:READ_OUTAGE', kind='REVIEW_REQUIRED',
                    message=f'策略 {sid}: 首次尝试及快速重试3次后仍未完成；{reason}。已转为每5分钟自动续办，成功后报告回执。')
            ledger.update_execution_job(sid, state=state, now=finished_at.isoformat(),
                next_attempt_at=(finished_at+timedelta(seconds=delay)).isoformat(), error=reason, started=started,
                notification=notification)

        if not strategy or strategy.state != 'ACTIVE':
            finish('SUPERSEDED','已被新策略替换，不执行旧任务')
            continue
        confirmed = {'LIVE','PARTIALLY_FILLED','FILLED','CLOSED'}
        if attempts and all(a.state in confirmed and a.ord_id for a in attempts):
            checks = [service._reconciler().reconcile(a.attempt_id) for a in attempts]
            if all(c.status.value in confirmed for c in checks) and not ledger.automation_pause_reason():
                finish('DONE','所有方向订单及止盈止损均已核验')
            else:
                finish('RECONCILING','订单存在，但止盈止损尚未闭环',delay=30)
            continue
        if states & {'SUBMITTING','SUBMITTED','UNKNOWN','CANCELING','CANCEL_UNKNOWN'}:
            finish('RECONCILING','等待订单对账，禁止盲重试',delay=30)
            continue
        if states - ({'READY'} | confirmed):
            finish('BLOCKED','订单或保护状态需要处理')
            continue
        # Intake already authenticated and accepted this message. Its ingress
        # freshness deadline must not consume the durable retry budget during
        # slow reads, queueing or a restart. Every attempt still checks ACTIVE,
        # current positions/prices and protection; terminal historical jobs are
        # excluded by due_execution_jobs and are never revived here.
        if not ledger.strategy_has_authenticated_ready_ingest(sid):
            finish('BLOCKED','缺少已认证的原始策略审计')
            continue
        finish('RUNNING',started=True)
        try:
            result = service._execute_unlocked(sid)
        except Exception as exc:
            # Unknown defects are visible; do not replay potential side effects.
            updated = {a.state for a in ledger.list_attempts(sid)}
            state = 'RECONCILING' if updated & {'SUBMITTING','SUBMITTED','UNKNOWN','CANCELING','CANCEL_UNKNOWN'} else 'BLOCKED'
            finish(state,f'执行异常 {type(exc).__name__}',delay=30)
            continue
        outcomes.append((sid,result))
        if result.status is ExecutionStatus.RETRYABLE:
            delay = RETRY_DELAYS[job['attempts']] if job['attempts'] < len(RETRY_DELAYS) else OUTAGE_RETRY_DELAY
            finish('RETRY',result.reason,delay=delay)
        elif result.status is ExecutionStatus.UNKNOWN:
            finish('RECONCILING',result.reason,delay=30)
        elif result.status is ExecutionStatus.SUBMITTED:
            rows=ledger.list_attempts(sid)
            if rows and all(a.ord_id and a.state in confirmed for a in rows):
                finish('DONE','所有方向已取得并核验订单回执')
            else:
                finish('RECONCILING','提交状态尚未闭环',delay=30)
        else:
            finish('BLOCKED',result.reason)
    return tuple(outcomes)
