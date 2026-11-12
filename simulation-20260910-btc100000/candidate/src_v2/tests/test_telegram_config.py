import unittest

from src_v2.telegram_config import load_telegram_api_credentials


class TelegramConfigTests(unittest.TestCase):
    def test_credentials_must_be_explicitly_provided(self):
        with self.assertRaisesRegex(RuntimeError, "TG_API_ID"):
            load_telegram_api_credentials({})

    def test_explicit_credentials_are_parsed_without_defaults(self):
        api_id, api_hash = load_telegram_api_credentials(
            {"TG_API_ID": "12345", "TG_API_HASH": "test-hash"}
        )

        self.assertEqual(12345, api_id)
        self.assertEqual("test-hash", api_hash)


if __name__ == "__main__":
    unittest.main()
