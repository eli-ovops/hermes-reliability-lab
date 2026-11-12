import os
import subprocess
import unittest
from pathlib import Path


DEFAULT_BOT = (
    Path(__file__).resolve().parents[1]
    / "bot_okx.py"
)
PYTHON = Path(
    "/Users/USER/乔帮主项目_移交包/qiaobangzhu/.venv/bin/python"
)


class LiveTradingLockTest(unittest.TestCase):
    def test_direct_start_fails_closed_before_any_okx_request(self):
        bot_path = Path(
            os.environ.get("BOT_OKX_UNDER_TEST", str(DEFAULT_BOT))
        )
        env = os.environ.copy()
        for name in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_SECRET_KEY", "OKX_PASSPHRASE"):
            env.pop(name, None)

        result = subprocess.run(
            [str(PYTHON), str(bot_path)],
            cwd=str(bot_path.parent),
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 78, output)
        self.assertIn("LIVE_TRADING_DISABLED", output)
        self.assertNotIn("HTTP Request:", output)
        self.assertNotIn("/api/v5/", output)


if __name__ == "__main__":
    unittest.main()
