import unittest
from unittest.mock import patch
import protect_once as protect

class ProtectCommandTests(unittest.TestCase):
    def test_no_flag_does_not_deploy_or_amend(self):
        with patch.object(protect.deploy_once,'check_config',return_value=(None,None,[],False)), patch.object(protect.deploy_once,'apply_deployment') as deploy, patch.object(protect,'remediation') as repair, patch('sys.argv',['protect_once.py']):
            protect.main()
        deploy.assert_not_called()
        repair.assert_not_called()

    def test_six_verified_results_complete_without_resuming(self):
        def deploy(*args,**kwargs):
            kwargs['before_restart']('fixture')
        with patch.object(protect.deploy_once,'check_config',return_value=(None,None,[],False)), patch.object(protect.deploy_once,'apply_deployment',side_effect=deploy), patch.object(protect,'remediation',return_value=[{'status':'CANCELED_NO_FILL'}]*6), patch('sys.argv',['protect_once.py','--apply','--cancel-unprotected']):
            protect.main()

    def test_any_unverified_result_fails_command(self):
        def deploy(*args,**kwargs):
            kwargs['before_restart']('fixture')
        with patch.object(protect.deploy_once,'check_config',return_value=(None,None,[],False)), patch.object(protect.deploy_once,'apply_deployment',side_effect=deploy), patch.object(protect,'remediation',return_value=[{'status':'CANCELED_NO_FILL'}]*5+[{'status':'BLOCKED'}]), patch('sys.argv',['protect_once.py','--apply','--cancel-unprotected']):
            with self.assertRaises(SystemExit) as error:
                protect.main()
            self.assertEqual(2,error.exception.code)
