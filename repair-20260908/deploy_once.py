"""User-operated deployment helper. No deployment occurs without --apply.

Updates only the verified candidate's changed production Python modules.
Never loads credentials, submits orders, resumes pauses, or replays old READY rows.
Starting the existing LaunchAgent enables its configured live trading behavior.
"""
import argparse
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
from datetime import datetime

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


def check_config():
    baseline = json.loads((ROOT/'baseline.json').read_text())
    manifest = json.loads((ROOT/'candidate_sha256.json').read_text())
    source = Path(baseline['source'])
    cfg = plistlib.loads(PLIST.read_bytes())
    if cfg.get('Label') != LABEL or Path(cfg['WorkingDirectory']).resolve() != source.parent.resolve():
        raise RuntimeError('LaunchAgent 与项目基线不一致')
    expected_python = source.parent/'.venv/bin/python'
    if cfg['ProgramArguments'] != [str(expected_python), '-m', 'src_v2.live_bootstrap']:
        raise RuntimeError('启动入口发生变化，停止部署')
    if Path(sys.executable).resolve() != expected_python.resolve():
        raise RuntimeError('请使用命令中指定的项目 Python')
    for rel, sha in manifest.items():
        p = ROOT/rel
        if p.is_symlink() or digest(p) != sha:
            raise RuntimeError(f'候选校验失败: {rel}')
    validation = json.loads((ROOT/'validation.json').read_text())
    if not validation.get('successful') or validation.get('tests',0) < 218:
        raise RuntimeError('离线验收未通过')
    ledger = Path(cfg['EnvironmentVariables']['V2_LEDGER_PATH'])
    if ledger.is_symlink() or not ledger.is_file() or ledger.parent.resolve()!=source.parent.resolve():
        raise RuntimeError('账本位置与项目不匹配')
    changed = [rel for rel in json.loads((ROOT/'changes.json').read_text())['changed'] if '/' not in rel and rel.endswith('.py')]
    already = all((source/rel).is_file() and digest(source/rel)==manifest['src_v2/'+rel] for rel in changed)
    if not already:
        for rel, sha in baseline['sha256'].items():
            p=source/rel
            if p.is_symlink() or not p.is_file() or digest(p)!=sha:
                raise RuntimeError(f'原源码已改变，不能覆盖: {rel}')
        for rel in changed:
            if rel not in baseline['sha256'] and (source/rel).exists():
                raise RuntimeError(f'新增文件位置已有内容: {rel}')
    with sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True) as c:
        version=c.execute('PRAGMA user_version').fetchone()[0]
        if version not in (3,4):raise RuntimeError('不支持当前账本版本')
        row=c.execute("SELECT state FROM runtime_controls WHERE control_key='automation'").fetchone()
        if not row or row[0]!='RUNNING':raise RuntimeError('自动化处于暂停状态；部署脚本不会擅自解除暂停')
    if already and version != 4:
        raise RuntimeError('代码已更新但账本升级未完成；请保留此前备份，停止重复部署')
    return source, ledger, changed, already


def apply_deployment(source, ledger, changed):
    backup=ROOT/'deploy-backups'/datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    if shutil.disk_usage(ROOT).free < max(100*1024*1024, ledger.stat().st_size*3):
        raise RuntimeError('备份可用空间不足')
    backup.mkdir(parents=True,mode=0o700)
    print(f'备份位置：{backup}',flush=True)
    loaded, old_pid, _=process_state()
    if loaded:
        run('launchctl','bootout',f'gui/{os.getuid()}/{LABEL}')
    for _ in range(100):
        if old_pid is None:break
        try:os.kill(old_pid,0)
        except ProcessLookupError:break
        time.sleep(0.1)
    else:raise RuntimeError('原进程尚未退出；未更新代码')
    if process_state()[0]:raise RuntimeError('服务仍处于已加载状态；未更新代码')
    # Acquire the same process lock before touching the ledger/code.
    sys.path.insert(0,str(ROOT))
    from src_v2.process_lock import ExclusiveProcessLock
    cfg=plistlib.loads(PLIST.read_bytes())
    lock=Path(cfg['EnvironmentVariables'].get('V2_LOCK_PATH',str(source.parent/'v2_executor.lock')))
    with ExclusiveProcessLock(lock):
        with sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True) as src:
            with sqlite3.connect(backup/'ledger.sqlite3') as dst:
                src.backup(dst)
                if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                    raise RuntimeError('账本备份完整性检查失败')
        os.chmod(backup/'ledger.sqlite3',0o600)
        originals=backup/'src_v2';originals.mkdir()
        for rel in changed:
            old=source/rel
            if old.exists():shutil.copy2(old,originals/rel)
        (backup/'manifest.json').write_text(json.dumps({'changed':changed,'ledger':str(ledger),'source':str(source)},indent=2))
        for rel in changed:
            target=source/rel
            temp=source/(rel+'.deploy-'+str(os.getpid()))
            with temp.open('xb') as f:f.write((ROOT/'src_v2'/rel).read_bytes());f.flush();os.fsync(f.fileno())
            os.chmod(temp,target.stat().st_mode & 0o777 if target.exists() else 0o600)
            os.replace(temp,target)
        # Only database schema changes here. No gateway/client is constructed.
        from src_v2.ledger import Ledger
        Ledger(ledger)
        with sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True) as c:
            if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('升级后账本完整性检查失败')
            if c.execute('PRAGMA user_version').fetchone()[0]!=4:raise RuntimeError('账本升级未完成')
    run('launchctl','bootstrap',f'gui/{os.getuid()}',str(PLIST))
    run('launchctl','kickstart',f'gui/{os.getuid()}/{LABEL}')
    observed_pid=None
    for _ in range(10):
        time.sleep(1)
        loaded,pid,running=process_state()
        if not loaded or not pid or not running:raise RuntimeError('服务未持续运行，请保留备份和现场，不要重复执行')
        if observed_pid is not None and pid!=observed_pid:raise RuntimeError('服务发生重复重启，请保留现场')
        observed_pid=pid
    (backup/'deployment.json').write_text(json.dumps({'pid':observed_pid,'schema':4,'service_running':True,'exchange_acceptance':'NOT_VERIFIED'},indent=2))
    print(f'部署完成；服务持续运行，PID={observed_pid}。未补挂历史记录；交易所订单回执尚待验收。')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true',help='停止并更新既有实盘服务，再启动；由用户本人执行')
    args=parser.parse_args()
    source,ledger,changed,already=check_config()
    if already:
        loaded,pid,running=process_state()
        if not loaded or not pid or not running:
            raise RuntimeError('代码已是候选版本，但服务未运行；需要检查此前部署记录')
        print('代码已是该候选版本且服务在运行；没有重复重启或再次部署。')
        return
    if not args.apply:
        print(f'只读预检通过；待更新 {len(changed)} 个模块。未部署。')
        return
    apply_deployment(source,ledger,changed)

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(f'部署未完成：{type(exc).__name__}: {exc}\n若服务已停止，请保留现场和 deploy-backups，不要反复重跑或覆盖账本。',file=sys.stderr)
        raise SystemExit(1)
