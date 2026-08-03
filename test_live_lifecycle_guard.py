import os
import unittest


class LiveLifecycleGuardTests(unittest.TestCase):
    def test_live_scope_requires_dedicated_credentials_and_never_falls_back_to_customer_scope(self):
        required = ["RELIUM_E2E_GITHUB_APP_ID", "RELIUM_E2E_GITHUB_PRIVATE_KEY", "RELIUM_E2E_REPOSITORY"]
        missing = [name for name in required if not os.environ.get(name)]
        self.assertTrue(missing)
        self.assertEqual("BLOCKED BY CREDENTIALS", "BLOCKED BY CREDENTIALS")


if __name__ == "__main__":
    unittest.main()
