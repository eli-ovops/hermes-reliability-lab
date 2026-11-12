"""One-command, user-operated deployment of the tested Sept 11 repair.

The default is a read-only preflight. --apply restarts the configured live
service; it never resumes a pause or changes/replays historical execution jobs.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
PLIST = Path('/Users/USER/Library/LaunchAgents/com.bangzhu.v2.plist')
LABEL = 'com.bangzhu.v2'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(*args, required=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if required and result.returncode:
        raise RuntimeError(f'{args[0]} {args[1]} 失败，退出码 {result.returncode}')
    return result


def process_state():
    result = run('launchctl', 'print', f'gui/{os.getuid()}/{LABEL}', required=False)
    pid = re.search(r'^\s*pid = (\d+)\s*$', result.stdout, re.M)
    running = re.search(r'^\s*state = running\s*$', result.stdout, re.M)
    return result.returncode == 0, int(pid.group(1)) if pid else None, bool(running)


def read_ledger_state(ledger):
    with sqlite3.connect(ledger.as_uri()+'?mode=ro', uri=True) as connection:
        connection.execute('PRAGMA query_only=ON')
        if connection.execute('PRAGMA user_version').fetchone()[0] != 4:
            raise RuntimeError('账本版本已变化，未修改数据库')
        row = connection.execute("SELECT state FROM runtime_controls WHERE control_key='automation'").fetchone()
        if row is None or row[0] not in {'RUNNING', 'PAUSED'}:
            raise RuntimeError('自动化开关状态无法确认')
        return row[0]


def check_config():
    baseline = json.loads((ROOT/'baseline.json').read_text())
    manifest = json.loads((ROOT/'candidate_sha256.json').read_text())
    source = Path(baseline['source'])
    cfg = plistlib.loads(PLIST.read_bytes())
    if cfg.get('Label') != LABEL or Path(cfg['WorkingDirectory']).resolve() != source.parent.resolve():
        raise RuntimeError('LaunchAgent 与项目基线不一致')
    python = source.parent/'.venv/bin/python'
    if cfg['ProgramArguments'] != [str(python), '-m', 'src_v2.live_bootstrap']:
        raise RuntimeError('启动入口已变化')
    if Path(sys.executable).resolve() != python.resolve():
        raise RuntimeError('请使用命令中指定的项目 Python')
    for rel, sha in manifest.items():
        path = ROOT/rel
        if path.is_symlink() or not path.is_file() or digest(path) != sha:
            raise RuntimeError(f'候选文件校验失败: {rel}')
    validation = json.loads((ROOT/'validation.json').read_text())
    tree_sha = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    if not validation.get('successful') or validation.get('tests', 0) < 252 or validation.get('tree_sha256') != tree_sha:
        raise RuntimeError('当前候选版本没有通过对应的离线验收')
    changed = json.loads((ROOT/'changes.json').read_text())['changed']
    expected_changed = [rel for rel, sha in baseline['sha256'].items() if manifest['src_v2/'+rel] != sha]
    if sorted(changed) != sorted(expected_changed) or any('/' in rel or not rel.endswith('.py') for rel in changed):
        raise RuntimeError('部署文件清单与基线不符')
    already = all((source/rel).is_file() and digest(source/rel) == manifest['src_v2/'+rel] for rel in changed)
    for rel, sha in baseline['sha256'].items():
        expected = manifest['src_v2/'+rel] if already else sha
        path = source/rel
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise RuntimeError(f'线上源码已被改动，不能覆盖: {rel}')
    ledger = Path(cfg['EnvironmentVariables']['V2_LEDGER_PATH'])
    if ledger.is_symlink() or not ledger.is_file() or ledger.parent.resolve() != source.parent.resolve():
        raise RuntimeError('账本位置与项目不匹配')
    read_ledger_state(ledger)
    return source, ledger, changed, already


def apply_deployment(source, ledger, changed):
    backup = ROOT/'deploy-backups'/datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    if shutil.disk_usage(ROOT).free < max(100*1024*1024, ledger.stat().st_size*3):
        raise RuntimeError('备份空间不足')
    backup.mkdir(parents=True, mode=0o700)
    print(f'备份位置：{backup}', flush=True)
    loaded, old_pid, _ = process_state()
    if loaded:
        run('launchctl', 'bootout', f'gui/{os.getuid()}/{LABEL}')
    for _ in range(100):
        if old_pid is None:
            break
        try:
            os.kill(old_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        raise RuntimeError('原进程尚未退出；未更新代码')
    if process_state()[0]:
        raise RuntimeError('服务仍处于加载状态；未更新代码')
    from src_v2.process_lock import ExclusiveProcessLock
    cfg = plistlib.loads(PLIST.read_bytes())
    lock = Path(cfg['EnvironmentVariables'].get('V2_LOCK_PATH', str(source.parent/'v2_executor.lock')))
    with ExclusiveProcessLock(lock):
        original_state = read_ledger_state(ledger)
        with sqlite3.connect(ledger.as_uri()+'?mode=ro', uri=True) as src:
            with sqlite3.connect(backup/'ledger.sqlite3') as dst:
                src.backup(dst)
                if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('账本备份不完整')
        os.chmod(backup/'ledger.sqlite3', 0o600)
        originals = backup/'src_v2'
        originals.mkdir()
        for rel in changed:
            shutil.copy2(source/rel, originals/rel)
        (backup/'manifest.json').write_text(json.dumps({'changed':changed, 'ledger':str(ledger),
            'source':str(source), 'automation_state':original_state}, indent=2))
        for rel in changed:
            target = source/rel
            temp = source/(rel+'.deploy-'+str(os.getpid()))
            with temp.open('xb') as handle:
                handle.write((ROOT/'src_v2'/rel).read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, target.stat().st_mode & 0o777)
            os.replace(temp, target)
        # No migration and no runtime-control/job writes in this deployment.
        if read_ledger_state(ledger) != original_state:
            raise RuntimeError('自动化开关在部署期间变化，请检查现场')
    run('launchctl', 'bootstrap', f'gui/{os.getuid()}', str(PLIST))
    run('launchctl', 'kickstart', f'gui/{os.getuid()}/{LABEL}')
    observed_pid = None
    for _ in range(10):
        time.sleep(1)
        loaded, pid, running = process_state()
        if not loaded or not pid or not running:
            raise RuntimeError('服务没有持续运行，请保留备份和现场')
        if observed_pid is not None and pid != observed_pid:
            raise RuntimeError('服务发生重复重启，请检查现场')
        observed_pid = pid
    report = {'pid':observed_pid, 'service_running':True, 'previous_automation_state':original_state,
              'current_automation_state':read_ledger_state(ledger), 'historical_jobs_replayed':False,
              'exchange_acceptance':'NOT_VERIFIED'}
    (backup/'deployment.json').write_text(json.dumps(report, indent=2))
    print(f"部署完成；服务 PID={observed_pid}；自动化状态={report['current_automation_state']}。")
    print('未补挂历史记录。新策略的实盘订单和止盈止损回执仍需实际验收；微信若仍限流，会继续退避重试。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='由用户执行：备份、替换修复文件并重启现有实盘服务')
    args = parser.parse_args()
    source, ledger, changed, already = check_config()
    if already:
        loaded, pid, running = process_state()
        if not loaded or not pid or not running:
            raise RuntimeError('代码已更新，但服务未运行；请检查前次部署记录')
        print('当前修复版本已部署且服务在运行；没有重复重启。')
    elif not args.apply:
        print(f'只读预检通过；待更新 {len(changed)} 个模块。自动化开关将保持原状态。未部署。')
    else:
        apply_deployment(source, ledger, changed)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'部署未完成：{type(exc).__name__}: {exc}\n请保留 deploy-backups 和现场，不要反复重跑或覆盖账本。', file=sys.stderr)
        raise SystemExit(1)
