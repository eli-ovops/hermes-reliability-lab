import tempfile
import unittest
from pathlib import Path

from src_v2.process_lock import ExclusiveProcessLock, SingleInstanceBusy


class ProcessLockBehaviorTests(unittest.TestCase):
    def test_second_executor_is_rejected_while_first_holds_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "executor.lock"

            with ExclusiveProcessLock(path):
                with self.assertRaises(SingleInstanceBusy):
                    with ExclusiveProcessLock(path):
                        self.fail("second executor entered critical section")

            with ExclusiveProcessLock(path):
                pass


if __name__ == "__main__":
    unittest.main()
