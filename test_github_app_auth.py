import base64
import json
import unittest
from unittest.mock import Mock


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
        client.create_installation_access_token.return_value = {"token": "installation"}
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
                    "permissions": {"issues": "write", "checks": "write"},
                }
                self.assertEqual(get_installation_token(client, 9, "jwt"), token)

    def test_invalid_ids_and_keys_are_rejected(self):
        from agent.github_app.auth import AuthenticationError, create_app_jwt

        with self.assertRaises(AuthenticationError):
            create_app_jwt(0, "key", signer=Mock())
        with self.assertRaises(AuthenticationError):
            create_app_jwt(1, "", signer=Mock())


if __name__ == "__main__":
    unittest.main()
