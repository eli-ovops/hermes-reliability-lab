"""All launchctl/process actions are mocked; apply only to temporary fixtures."""
import json
from pathlib import Path
import plistlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import deploy_once as deploy
from src_v2.ledger import Ledger

class DeployTests(unittest.TestCase):
    def test_default_invocation_never_applies(self):
        with patch.object(deploy,'check_config',return_value=(None,None,['a.py'],False)),patch.object(deploy,'apply_deployment') as apply,patch('sys.argv',['deploy_once.py']):
            deploy.main()
        apply.assert_not_called()

    def test_user_apply_updates_only_temporary_fixture_and_backs_up_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);candidate=root/'candidate';project=root/'project';source=project/'src_v2'
            source.mkdir(parents=True);(candidate/'src_v2').mkdir(parents=True)
            (source/'example.py').write_text('old = True\n');(candidate/'src_v2/example.py').write_text('new = True\n')
            ledger=project/'ledger.sqlite3';Ledger(ledger)
            with sqlite3.connect(ledger) as c:c.execute('PRAGMA user_version=3')
            plist=root/'test.plist';plist.write_bytes(plistlib.dumps({'EnvironmentVariables':{'V2_LOCK_PATH':str(project/'executor.lock')}}))
            states=[(True,None,True),(False,None,False)]+[(True,123,True)]*10
            with patch.object(deploy,'ROOT',candidate),patch.object(deploy,'PLIST',plist),patch.object(deploy,'process_state',side_effect=states),patch.object(deploy,'run') as command,patch.object(deploy.time,'sleep'):
                deploy.apply_deployment(source,ledger,['example.py'])
            backups=list((candidate/'deploy-backups').iterdir());self.assertEqual(1,len(backups));backup=backups[0]
            self.assertEqual('old = True\n',(backup/'src_v2/example.py').read_text())
            self.assertEqual('new = True\n',(source/'example.py').read_text())
            with sqlite3.connect(backup/'ledger.sqlite3') as c:self.assertEqual(3,c.execute('PRAGMA user_version').fetchone()[0])
            with sqlite3.connect(ledger) as c:self.assertEqual(4,c.execute('PRAGMA user_version').fetchone()[0])
            self.assertEqual(['bootout','bootstrap','kickstart'],[call.args[1] for call in command.call_args_list])
            self.assertEqual('NOT_VERIFIED',json.loads((backup/'deployment.json').read_text())['exchange_acceptance'])

    def test_failed_stop_does_not_write_code_or_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'src';source.mkdir();(source/'example.py').write_text('old')
            ledger=root/'ledger';ledger.write_bytes(b'fixture')
            with patch.object(deploy,'ROOT',root),patch.object(deploy,'process_state',return_value=(True,None,True)),patch.object(deploy,'run',side_effect=RuntimeError('stop failed')) as command:
                with self.assertRaises(RuntimeError):deploy.apply_deployment(source,ledger,['example.py'])
            self.assertEqual('old',(source/'example.py').read_text());self.assertEqual(1,command.call_count)
