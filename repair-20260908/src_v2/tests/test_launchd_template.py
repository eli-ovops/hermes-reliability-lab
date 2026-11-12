import plistlib
import unittest
from pathlib import Path


TEMPLATE = (
    Path(__file__).parents[1]
    / "launchd"
    / "com.bangzhu.v2.plist.template"
)


class LaunchAgentTemplateTests(unittest.TestCase):
    def test_template_is_single_instance_shadow_and_contains_no_credentials(self):
        data = plistlib.loads(TEMPLATE.read_bytes())

        self.assertEqual("com.bangzhu.v2", data["Label"])
        self.assertTrue(data["KeepAlive"])
        self.assertFalse(data["RunAtLoad"])
        self.assertNotIn("StartInterval", data)
        self.assertEqual("shadow", data["ProgramArguments"][-1])
        serialized = TEMPLATE.read_text()
        self.assertNotIn("API_SECRET", serialized)
        self.assertNotIn("PASSPHRASE", serialized)
        self.assertNotIn("TG_API_HASH", serialized)


if __name__ == "__main__":
    unittest.main()
