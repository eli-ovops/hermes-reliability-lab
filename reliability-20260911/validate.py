"""Offline acceptance, including network denial and deploy process mocks."""
import hashlib
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent


def main():
    suite = unittest.defaultTestLoader.discover(str(ROOT/'src_v2/tests'))
    with (ROOT/'validation.log').open('w') as log, patch.object(socket.socket, 'connect', side_effect=AssertionError('Offline test attempted network')):
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    deploy_suite = unittest.defaultTestLoader.loadTestsFromName('test_deploy_once')
    with (ROOT/'deploy-validation.log').open('w') as log, patch.object(socket.socket, 'connect', side_effect=AssertionError('Offline test attempted network')):
        deploy_result = unittest.TextTestRunner(stream=log, verbosity=2).run(deploy_suite)
    if not result.wasSuccessful() or not deploy_result.wasSuccessful():
        print('Validation failed; see validation.log and deploy-validation.log')
        return 1
    from replay_today import main as replay
    replay()
    manifest = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted((ROOT/'src_v2').rglob('*.py'))}
    for name in ('today-fixture.json', 'replay_today.py', 'test_deploy_once.py'):
        manifest[name] = hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
    (ROOT/'candidate_sha256.json').write_text(json.dumps(manifest, indent=2))
    baseline = json.loads((ROOT/'baseline.json').read_text())
    changed = [name for name, sha in baseline['sha256'].items() if manifest['src_v2/'+name] != sha]
    (ROOT/'changes.json').write_text(json.dumps({'changed':changed}, indent=2))
    report = {'successful':True, 'tests':result.testsRun, 'deployment_tests':deploy_result.testsRun,
        'offline_replay_passed':True, 'network_disabled_in_tests':True,
        'tree_sha256':hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()}
    (ROOT/'validation.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
    print('Changed production modules:', ', '.join(changed))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
