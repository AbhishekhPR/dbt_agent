from __future__ import annotations

import unittest
from datetime import datetime, timezone

from agent.api.clerk_identity import ClerkPrincipal
from agent.lifecycle_authorization import (
    RecentVerificationRequired,
    require_recent_verification,
)


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _principal(**overrides):
    values = {
        "clerk_user_id": "user_test",
        "clerk_organization_id": "org_test",
        "tenant_id": "ten_test",
        "factor_verification_age": (0, -1),
        "clerk_token_issued_at": NOW,
        "is_impersonated": False,
    }
    values.update(overrides)
    return ClerkPrincipal(**values)


class RecentVerificationTests(unittest.TestCase):
    def test_accepts_recent_strongest_available_factor(self):
        require_recent_verification(_principal(factor_verification_age=(9, -1)))
        require_recent_verification(_principal(factor_verification_age=(9, 9)))

    def test_rejects_missing_or_stale_first_factor(self):
        for age in (None, (-1, -1), (10, -1), (11, 0)):
            with self.subTest(age=age):
                with self.assertRaises(RecentVerificationRequired):
                    require_recent_verification(_principal(
                        factor_verification_age=age))

    def test_rejects_stale_second_factor_when_one_exists(self):
        with self.assertRaises(RecentVerificationRequired):
            require_recent_verification(_principal(
                factor_verification_age=(0, 10)))

    def test_rejects_impersonated_sessions(self):
        with self.assertRaises(RecentVerificationRequired) as raised:
            require_recent_verification(_principal(is_impersonated=True))
        self.assertEqual(raised.exception.category, "impersonated_session")


if __name__ == "__main__":
    unittest.main()
