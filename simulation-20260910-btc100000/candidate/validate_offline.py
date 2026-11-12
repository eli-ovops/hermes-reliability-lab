"""Run candidate tests with outbound connections disabled; no live ledger is opened."""
import json
import socket
import unittest
from pathlib import Path
from unittest.mock import patch

root=Path(__file__).resolve().parent
suite=unittest.defaultTestLoader.discover(str(root/'src_v2/tests'),top_level_dir=str(root))
with patch.object(socket.socket,'connect',side_effect=AssertionError('offline validation forbids network')), patch.object(socket,'create_connection',side_effect=AssertionError('offline validation forbids network')):
    result=unittest.TextTestRunner(verbosity=1).run(suite)
summary={'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'successful':result.wasSuccessful(),'network':'disabled','live_orders_submitted':False}
(root/'validation.json').write_text(json.dumps(summary,indent=2)+'\n')
raise SystemExit(not result.wasSuccessful())
