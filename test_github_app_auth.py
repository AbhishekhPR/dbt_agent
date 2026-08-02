import base64
import json
import unittest
from unittest.mock import Mock


APPROVED_INSTALLATION_PERMISSIONS = {
    "checks": "write",
    "contents": "read",
    "issues": "write",
    "metadata": "read",
    "pull_requests": "write",
}


class GitHubAppAuthTests(unittest.TestCase):
    def test_jwt_has_rs256_short_lived_claims_and_uses_signer(self):
        from agent.github_app.auth import create_app_jwt

        signer = Mock(return_value=b"signature")
        token = create_app_jwt(123, "key", now=1000, signer=signer)
        header, claims, signature = token.split(".")
        decode = lambda value: json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        self.assertEqual(decode(header), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(decode(claims), {"exp": 1540, "iat": 940, "iss": "123"})
        self.assertEqual(signature, "c2lnbmF0dXJl")
        signer.assert_called_once()

    def test_installation_token_is_required(self):
        from agent.github_app.auth import AuthenticationError, get_installation_token

        client = Mock()
        client.create_installation_access_token.return_value = {
            "token": "installation",
            "permissions": APPROVED_INSTALLATION_PERMISSIONS,
        }
        self.assertEqual(get_installation_token(client, 9, "jwt"), "installation")
        client.create_installation_access_token.return_value = {}
        with self.assertRaises(AuthenticationError):
            get_installation_token(client, 9, "jwt")

    def test_installation_token_is_an_opaque_non_empty_string(self):
        from agent.github_app.auth import get_installation_token

        tokens = (
            "traditional-installation-token",
            "ghs_1234567890_eyJhbGciOiJSUzI1NiJ9.payload.signature",
            "token_with_underscores",
            "token.with.periods",
        )
        for token in tokens:
            with self.subTest(token=token):
                client = Mock()
                client.create_installation_access_token.return_value = {
                    "token": token,
                    "permissions": APPROVED_INSTALLATION_PERMISSIONS,
                }
                self.assertEqual(get_installation_token(client, 9, "jwt"), token)

    def test_publication_token_has_exact_approved_minimal_permissions(self):
        from agent.github_app.auth import (
            REQUIRED_INSTALLATION_TOKEN_PERMISSIONS,
            get_installation_token,
        )

        self.assertEqual(
            REQUIRED_INSTALLATION_TOKEN_PERMISSIONS,
            APPROVED_INSTALLATION_PERMISSIONS,
        )
        self.assertEqual(
            REQUIRED_INSTALLATION_TOKEN_PERMISSIONS["issues"], "write"
        )
        self.assertEqual(
            REQUIRED_INSTALLATION_TOKEN_PERMISSIONS["checks"], "write"
        )
        self.assertEqual(
            REQUIRED_INSTALLATION_TOKEN_PERMISSIONS["contents"], "read"
        )
        self.assertEqual(
            REQUIRED_INSTALLATION_TOKEN_PERMISSIONS["pull_requests"], "write"
        )
        client = Mock()
        client.create_installation_access_token.return_value = {
            "token": "installation-secret",
            "permissions": APPROVED_INSTALLATION_PERMISSIONS,
        }

        self.assertEqual(
            get_installation_token(client, 9, "app-jwt-secret"),
            "installation-secret",
        )
        client.create_installation_access_token.assert_called_once_with(
            9, "app-jwt-secret"
        )

    def test_missing_issues_write_fails_before_token_can_be_used(self):
        from agent.github_app.auth import AuthenticationError, get_installation_token

        client = Mock()
        permissions = dict(APPROVED_INSTALLATION_PERMISSIONS)
        permissions["issues"] = "read"
        client.create_installation_access_token.return_value = {
            "token": "installation-secret",
            "permissions": permissions,
        }

        with self.assertRaisesRegex(AuthenticationError, "issues:write") as raised:
            get_installation_token(client, 9, "app-jwt-secret")

        self.assertNotIn("installation-secret", str(raised.exception))
        self.assertNotIn("app-jwt-secret", str(raised.exception))

    def test_unapproved_or_missing_permissions_are_rejected_without_secrets(self):
        from agent.github_app.auth import AuthenticationError, get_installation_token

        cases = (
            ({}, "permission map"),
            (
                {**APPROVED_INSTALLATION_PERMISSIONS, "administration": "read"},
                "administration",
            ),
        )
        for permissions, expected_message in cases:
            with self.subTest(permissions=permissions):
                client = Mock()
                client.create_installation_access_token.return_value = {
                    "token": "installation-secret",
                    "permissions": permissions,
                }
                with self.assertRaisesRegex(
                    AuthenticationError, expected_message
                ) as raised:
                    get_installation_token(client, 9, "app-jwt-secret")
                representation = str(raised.exception)
                self.assertNotIn("installation-secret", representation)
                self.assertNotIn("app-jwt-secret", representation)

    def test_invalid_ids_and_keys_are_rejected(self):
        from agent.github_app.auth import AuthenticationError, create_app_jwt

        with self.assertRaises(AuthenticationError):
            create_app_jwt(0, "key", signer=Mock())
        with self.assertRaises(AuthenticationError):
            create_app_jwt(1, "", signer=Mock())


if __name__ == "__main__":
    unittest.main()
