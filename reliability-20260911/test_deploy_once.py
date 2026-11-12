"""Deployment uses temporary fixtures; all launchctl/process actions are mocked."""
import json
from pathlib import Path
import plistlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import deploy_once as deploy
from src_v2.ledger import Ledger


class DeploymentTests(unittest.TestCase):
    def test_default_invocation_does_not_apply(self):
        with patch.object(deploy,'check_config',return_value=(None,None,['x.py'],False)), patch.object(deploy,'apply_deployment') as apply, patch('sys.argv',['deploy_once.py']):
            deploy.main()
        apply.assert_not_called()

    def test_repeated_apply_does_not_restart_already_deployed_service(self):
        with patch.object(deploy,'check_config',return_value=(None,None,['x.py'],True)), patch.object(deploy,'process_state',return_value=(True,123,True)), patch.object(deploy,'apply_deployment') as apply, patch('sys.argv',['deploy_once.py','--apply']):
            deploy.main()
        apply.assert_not_called()

    def test_deployment_preserves_running_or_paused_state_and_historical_jobs(self):
        for state in ('RUNNING','PAUSED'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temp:
                root=Path(temp); candidate=root/'candidate'; source=root/'project/src_v2'
                source.mkdir(parents=True); (candidate/'src_v2').mkdir(parents=True)
                (source/'example.py').write_text('old = True\n')
                (candidate/'src_v2/example.py').write_text('new = True\n')
                ledger=source.parent/'ledger.sqlite3'; l=Ledger(ledger)
                l.resume_automation('fixture') if state=='RUNNING' else l.pause_automation('fixture')
                with sqlite3.connect(ledger) as c: before='\n'.join(c.iterdump())
                plist=root/'test.plist'; plist.write_bytes(plistlib.dumps({'EnvironmentVariables':{'V2_LOCK_PATH':str(source.parent/'lock')}}))
                states=[(True,None,True),(False,None,False)]+[(True,123,True)]*10
                with patch.object(deploy,'ROOT',candidate), patch.object(deploy,'PLIST',plist), patch.object(deploy,'process_state',side_effect=states), patch.object(deploy,'run') as command, patch.object(deploy.time,'sleep'):
                    deploy.apply_deployment(source,ledger,['example.py'])
                backup=next((candidate/'deploy-backups').iterdir())
                self.assertEqual('old = True\n',(backup/'src_v2/example.py').read_text())
                self.assertEqual('new = True\n',(source/'example.py').read_text())
                with sqlite3.connect(ledger) as c: self.assertEqual(before,'\n'.join(c.iterdump()))
                report=json.loads((backup/'deployment.json').read_text())
                self.assertEqual(state,report['current_automation_state'])
                self.assertFalse(report['historical_jobs_replayed'])
                self.assertEqual(['bootout','bootstrap','kickstart'],[call.args[1] for call in command.call_args_list])

    def test_failed_stop_leaves_source_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); source=root/'src'; source.mkdir(); (source/'x.py').write_text('old')
            ledger=root/'ledger'; ledger.write_bytes(b'fixture')
            with patch.object(deploy,'ROOT',root), patch.object(deploy,'process_state',return_value=(True,None,True)), patch.object(deploy,'run',side_effect=RuntimeError('stop failed')):
                with self.assertRaises(RuntimeError): deploy.apply_deployment(source,ledger,['x.py'])
            self.assertEqual('old',(source/'x.py').read_text())


if __name__ == '__main__': unittest.main()
