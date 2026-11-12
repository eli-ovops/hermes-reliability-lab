import os
from pathlib import Path
import tempfile
import unittest

from src_v2.live_bootstrap import prepare_live_environment


class LiveBootstrapTests(unittest.TestCase):
    def test_secure_legacy_sources_are_mapped_only_into_process_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            okx_env = root / "legacy.env"
            okx_env.write_text(
                "OKX_API_KEY=test-key\n"
                "OKX_API_SECRET=test-secret\n"
                "OKX_PASSPHRASE=test-pass\n"
            )
            tg_source = root / "legacy_tg.py"
            tg_source.write_text(
                "import os\n"
                "API_ID = int(os.environ.get('TG_API_ID', 12345))\n"
                "API_HASH = os.environ.get('TG_API_HASH', 'test-hash')\n"
            )
            os.chmod(okx_env, 0o600)
            os.chmod(tg_source, 0o600)
            env = {
                "V2_OKX_ENV_FILE": str(okx_env),
                "V2_LEGACY_TG_SOURCE_FILE": str(tg_source),
            }

            prepare_live_environment(env)

            self.assertEqual("test-key", env["V2_OKX_API_KEY"])
            self.assertEqual("test-secret", env["V2_OKX_API_SECRET"])
            self.assertEqual("test-pass", env["V2_OKX_PASSPHRASE"])
            self.assertEqual("12345", env["TG_API_ID"])
            self.assertEqual("test-hash", env["TG_API_HASH"])

    def test_group_readable_credential_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            okx_env = root / "legacy.env"
            okx_env.write_text(
                "OKX_API_KEY=test-key\n"
                "OKX_API_SECRET=test-secret\n"
                "OKX_PASSPHRASE=test-pass\n"
            )
            tg_source = root / "legacy_tg.py"
            tg_source.write_text("API_ID = 12345\nAPI_HASH = 'test-hash'\n")
            os.chmod(okx_env, 0o640)
            os.chmod(tg_source, 0o600)

            with self.assertRaisesRegex(RuntimeError, "0600"):
                prepare_live_environment(
                    {
                        "V2_OKX_ENV_FILE": str(okx_env),
                        "V2_LEGACY_TG_SOURCE_FILE": str(tg_source),
                    }
                )

    def test_legacy_okx_path_cannot_silently_supply_live_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            okx_env = root / "legacy.env"
            okx_env.write_text(
                "OKX_API_KEY=test-key\n"
                "OKX_API_SECRET=test-secret\n"
                "OKX_PASSPHRASE=test-pass\n"
            )
            tg_source = root / "legacy_tg.py"
            tg_source.write_text("API_ID = 12345\nAPI_HASH = 'test-hash'\n")
            os.chmod(okx_env, 0o600)
            os.chmod(tg_source, 0o600)

            with self.assertRaisesRegex(RuntimeError, "OKX 凭证文件"):
                prepare_live_environment(
                    {
                        "V2_LEGACY_OKX_ENV_FILE": str(okx_env),
                        "V2_LEGACY_TG_SOURCE_FILE": str(tg_source),
                    }
                )


if __name__ == "__main__":
    unittest.main()
