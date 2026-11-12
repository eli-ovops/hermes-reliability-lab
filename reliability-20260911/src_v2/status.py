"""Read-only local status; never constructs an exchange client or migrates a ledger."""
import argparse
import json
import sqlite3
from pathlib import Path


def read_status(path):
    p = Path(path).resolve(strict=True)
    c = sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)
    c.row_factory=sqlite3.Row
    try:
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        controls=[dict(r) for r in c.execute("SELECT state,reason,updated_at FROM runtime_controls WHERE control_key='automation'")]
        orders=[dict(r) for r in c.execute("SELECT symbol,direction,state,ord_id,entry_price,stop_price,target_price,updated_at FROM direction_attempts ORDER BY created_at DESC LIMIT 30")]
        jobs=[dict(r) for r in c.execute('SELECT * FROM execution_jobs ORDER BY updated_at DESC LIMIT 30')] if 'execution_jobs' in tables else []
        notices=[dict(r) for r in c.execute("SELECT notification_id,kind,state,attempt_count,updated_at FROM notifications WHERE state='PENDING' ORDER BY notification_id LIMIT 30")]
        return {'automation':controls,'jobs':jobs,'orders':orders,'pending_notifications':notices,'evidence':'local ledger snapshot; exchange state not queried'}
    finally:
        c.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger',required=True)
    args=parser.parse_args()
    print(json.dumps(read_status(args.ledger),ensure_ascii=False,indent=2))

if __name__=='__main__': main()
