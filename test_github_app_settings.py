import dataclasses
import tempfile
import unittest
from pathlib import Path


class GitHubAppSettingsTests(unittest.TestCase):
    def _environment(self, root, **overrides):
        key_path = Path(root) / "github-app.pem"
        key_path.write_text("test-private-key", encoding="utf-8")
        values = {
            "RELIUM_GITHUB_APP_ID": "123",
            "RELIUM_GITHUB_WEBHOOK_SECRET": "webhook-secret-value",
            "RELIUM_GITHUB_PRIVATE_KEY_PATH": str(key_path),
            "RELIUM_STORAGE_ROOT": str(Path(root) / "storage"),
        }
        values.update(overrides)
        return values

    def test_valid_settings_are_immutable_and_defaults_are_applied(self):
        from agent.github_app.settings import load_settings

        with tempfile.TemporaryDirectory() as root:
            settings = load_settings(self._environment(root))
        self.assertEqual(settings.app_id, 123)
        self.assertEqual(settings.worker_count, 2)
        self.assertEqual(settings.queue_capacity, 100)
        self.assertEqual(settings.max_retries, 3)
        self.assertEqual(settings.retry_base_seconds, 1.0)
        self.assertEqual(settings.host, "0.0.0.0")
        self.assertEqual(settings.port, 8000)
        self.assertEqual(settings.max_body_bytes, 2 * 1024 * 1024)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            settings.port = 9000

    def test_explicit_values_are_parsed(self):
        from agent.github_app.settings import load_settings

        with tempfile.TemporaryDirectory() as root:
            settings = load_settings(
                self._environment(
                    root,
                    RELIUM_WORKER_COUNT="4",
                    RELIUM_QUEUE_CAPACITY="12",
                    RELIUM_MAX_RETRIES="0",
                    RELIUM_RETRY_BASE_SECONDS="0.25",
                    RELIUM_HOST="127.0.0.1",
                    RELIUM_PORT="9000",
                    RELIUM_MAX_BODY_BYTES="4096",
                )
            )
        self.assertEqual(
            (
                settings.worker_count,
                settings.queue_capacity,
                settings.max_retries,
                settings.retry_base_seconds,
                settings.host,
                settings.port,
                settings.max_body_bytes,
            ),
            (4, 12, 0, 0.25, "127.0.0.1", 9000, 4096),
        )

    def test_missing_required_variables_fail_by_name(self):
        from agent.github_app.settings import SettingsError, load_settings

        required = (
            "RELIUM_GITHUB_APP_ID",
            "RELIUM_GITHUB_WEBHOOK_SECRET",
            "RELIUM_GITHUB_PRIVATE_KEY_PATH",
            "RELIUM_STORAGE_ROOT",
        )
        with tempfile.TemporaryDirectory() as root:
            for name in required:
                with self.subTest(name=name):
                    environment = self._environment(root)
                    environment.pop(name)
                    with self.assertRaisesRegex(SettingsError, name):
                        load_settings(environment)

    def test_invalid_numbers_fail_without_echoing_values(self):
        from agent.github_app.settings import SettingsError, load_settings

        invalid = {
            "RELIUM_GITHUB_APP_ID": "secret-invalid-app-id",
            "RELIUM_WORKER_COUNT": "0",
            "RELIUM_QUEUE_CAPACITY": "-1",
            "RELIUM_MAX_RETRIES": "-1",
            "RELIUM_RETRY_BASE_SECONDS": "0",
            "RELIUM_PORT": "70000",
            "RELIUM_MAX_BODY_BYTES": "0",
        }
        with tempfile.TemporaryDirectory() as root:
            for name, value in invalid.items():
                with self.subTest(name=name):
                    with self.assertRaises(SettingsError) as raised:
                        load_settings(self._environment(root, **{name: value}))
                    self.assertIn(name, str(raised.exception))
                    self.assertNotIn(value, str(raised.exception))

    def test_missing_private_key_is_safe(self):
        from agent.github_app.settings import SettingsError, load_settings

        missing = "/private/location/secret-app-key.pem"
        with tempfile.TemporaryDirectory() as root:
            environment = self._environment(
                root, RELIUM_GITHUB_PRIVATE_KEY_PATH=missing
            )
            with self.assertRaises(SettingsError) as raised:
                load_settings(environment)
        self.assertNotIn(missing, str(raised.exception))

    def test_secret_and_key_contents_are_not_in_repr(self):
        from agent.github_app.settings import load_settings

        with tempfile.TemporaryDirectory() as root:
            settings = load_settings(self._environment(root))
        rendered = repr(settings)
        self.assertNotIn("webhook-secret-value", rendered)
        self.assertNotIn("test-private-key", rendered)

    def test_storage_root_must_not_be_a_file(self):
        from agent.github_app.settings import SettingsError, load_settings

        with tempfile.TemporaryDirectory() as root:
            storage_file = Path(root) / "storage-file"
            storage_file.write_text("not a directory", encoding="utf-8")
            environment = self._environment(
                root, RELIUM_STORAGE_ROOT=str(storage_file)
            )
            with self.assertRaisesRegex(SettingsError, "RELIUM_STORAGE_ROOT"):
                load_settings(environment)


if __name__ == "__main__":
    unittest.main()
