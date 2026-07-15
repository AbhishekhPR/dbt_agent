import hashlib
import hmac
import unittest
from unittest.mock import patch


class GitHubAppSignatureTests(unittest.TestCase):
    def _signature(self, secret, body):
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return f"sha256={digest}"

    def test_accepts_valid_sha256_signature(self):
        from agent.github_app.signatures import verify_webhook_signature

        body = b'{"action":"opened"}'
        self.assertTrue(
            verify_webhook_signature(
                secret="webhook-secret",
                body=body,
                signature_header=self._signature("webhook-secret", body),
            )
        )

    def test_rejects_tampered_body_or_wrong_secret(self):
        from agent.github_app.signatures import verify_webhook_signature

        body = b'{"action":"opened"}'
        signature = self._signature("webhook-secret", body)
        self.assertFalse(
            verify_webhook_signature(
                secret="webhook-secret",
                body=body + b" ",
                signature_header=signature,
            )
        )
        self.assertFalse(
            verify_webhook_signature(
                secret="wrong-secret",
                body=body,
                signature_header=signature,
            )
        )

    def test_rejects_missing_malformed_and_legacy_headers(self):
        from agent.github_app.signatures import verify_webhook_signature

        for header in (None, "", "sha1=abc", "sha256=abc", "sha256=" + "z" * 64):
            with self.subTest(header=header):
                self.assertFalse(
                    verify_webhook_signature(
                        secret="webhook-secret",
                        body=b"{}",
                        signature_header=header,
                    )
                )

    def test_configuration_and_body_types_are_validated(self):
        from agent.github_app.signatures import SignatureConfigurationError, verify_webhook_signature

        with self.assertRaises(SignatureConfigurationError):
            verify_webhook_signature(secret="", body=b"{}", signature_header="sha256=" + "0" * 64)
        with self.assertRaises(TypeError):
            verify_webhook_signature(secret="secret", body="{}", signature_header="sha256=" + "0" * 64)

    def test_digest_is_compared_in_constant_time(self):
        from agent.github_app.signatures import verify_webhook_signature

        body = b"{}"
        signature = self._signature("secret", body)
        with patch("agent.github_app.signatures.hmac.compare_digest", return_value=True) as compare:
            self.assertTrue(
                verify_webhook_signature(
                    secret="secret",
                    body=body,
                    signature_header=signature,
                )
            )
        compare.assert_called_once()


if __name__ == "__main__":
    unittest.main()
