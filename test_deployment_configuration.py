"""Production configuration: the App private key, and who owns the storage root.

Two things a controlled pilot must not get wrong. A private key that only
works when it happens to be a file on disk cannot be deployed to a platform
that injects secrets as environment variables; and a filesystem that two
processes both believe they own will lose webhook idempotency the first time
they are scheduled apart.
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from agent.github_app.private_key import (
    INLINE_VAR, PATH_VAR, PrivateKeyError, resolve_private_key,
)
from agent.github_app.settings import SettingsError, load_settings


def _generate_pem() -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


PEM = _generate_pem()


class PrivateKeyResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.environ.get("TEMP", "/tmp")) / f"relium-key-{os.getpid()}.pem"
        self.tmp.write_bytes(PEM)
        self.addCleanup(lambda: self.tmp.unlink(missing_ok=True))

    def test_inline_pem_is_accepted(self):
        pem, source = resolve_private_key({INLINE_VAR: PEM.decode()})
        self.assertEqual(pem.strip(), PEM.strip())
        self.assertEqual(source, INLINE_VAR)

    def test_path_pem_is_accepted(self):
        pem, source = resolve_private_key({PATH_VAR: str(self.tmp)})
        self.assertEqual(pem.strip(), PEM.strip())
        self.assertEqual(source, PATH_VAR)

    def test_inline_wins_when_both_are_set(self):
        """The documented precedence, asserted rather than assumed.

        A stale path left in configuration must not outrank the secret the
        operator actually injected.
        """
        _pem, source = resolve_private_key({
            INLINE_VAR: PEM.decode(), PATH_VAR: str(self.tmp)})
        self.assertEqual(source, INLINE_VAR)

    def test_an_escaped_newline_pem_is_accepted(self):
        """Platforms that carry secrets through single-line fields."""
        escaped = PEM.decode().replace("\n", "\\n")
        pem, _source = resolve_private_key({INLINE_VAR: escaped})
        self.assertEqual(pem.strip(), PEM.strip())

    def test_a_real_newline_pem_is_left_alone(self):
        pem, _source = resolve_private_key({INLINE_VAR: PEM.decode()})
        self.assertNotIn(b"\\n", pem)

    def test_absent_key_is_refused_with_both_variable_names(self):
        with self.assertRaises(PrivateKeyError) as caught:
            resolve_private_key({})
        self.assertIn(INLINE_VAR, str(caught.exception))
        self.assertIn(PATH_VAR, str(caught.exception))

    def test_absent_key_may_be_optional(self):
        self.assertEqual(resolve_private_key({}, required=False), (None, None))

    def test_blank_values_are_treated_as_absent(self):
        with self.assertRaises(PrivateKeyError):
            resolve_private_key({INLINE_VAR: "   ", PATH_VAR: ""})

    def test_a_malformed_inline_key_fails_loudly(self):
        for bad in ("not a key at all",
                    "-----BEGIN PRIVATE KEY-----\nnot base64\n-----END PRIVATE KEY-----"):
            with self.assertRaises(PrivateKeyError):
                resolve_private_key({INLINE_VAR: bad})

    def test_an_unreadable_path_fails_loudly(self):
        with self.assertRaises(PrivateKeyError):
            resolve_private_key({PATH_VAR: str(self.tmp) + ".missing"})

    def test_an_empty_file_fails_loudly(self):
        empty = self.tmp.with_suffix(".empty")
        empty.write_bytes(b"")
        self.addCleanup(lambda: empty.unlink(missing_ok=True))
        with self.assertRaises(PrivateKeyError):
            resolve_private_key({PATH_VAR: str(empty)})

    def test_no_error_message_contains_key_material(self):
        """A configuration error is one of the easiest places for a secret to escape."""
        body = PEM.decode().splitlines()[1]
        cases = [
            {INLINE_VAR: "-----BEGIN PRIVATE KEY-----\n" + body + "\ntruncated"},
            {PATH_VAR: str(self.tmp) + ".missing"},
            {},
        ]
        for values in cases:
            try:
                resolve_private_key(values)
            except PrivateKeyError as exc:
                self.assertNotIn(body, str(exc))
                self.assertNotIn("-----BEGIN", str(exc).replace(
                    "no '-----BEGIN' header", ""))


class SettingsPrivateKeyTests(unittest.TestCase):
    """The same rule, through the real settings loader."""

    def _env(self, **overrides):
        base = {
            "RELIUM_GITHUB_APP_ID": "42",
            "RELIUM_GITHUB_WEBHOOK_SECRET": "s3cret",
            "RELIUM_STORAGE_ROOT": str(Path(os.environ.get("TEMP", "/tmp")) / "relium-store"),
        }
        base.update(overrides)
        return base

    def test_inline_key_configures_the_api(self):
        settings = load_settings(self._env(**{INLINE_VAR: PEM.decode()}))
        self.assertEqual(settings.private_key.strip(), PEM.strip())
        self.assertIsNone(settings.private_key_path,
                          "an inline key has no file behind it")

    def test_path_key_still_configures_the_api(self):
        tmp = Path(os.environ.get("TEMP", "/tmp")) / f"relium-settings-{os.getpid()}.pem"
        tmp.write_bytes(PEM)
        self.addCleanup(lambda: tmp.unlink(missing_ok=True))
        settings = load_settings(self._env(**{PATH_VAR: str(tmp)}))
        self.assertEqual(settings.private_key.strip(), PEM.strip())
        self.assertEqual(settings.private_key_path, tmp)

    def test_a_missing_key_stops_startup(self):
        with self.assertRaises(SettingsError):
            load_settings(self._env())

    def test_a_malformed_key_stops_startup(self):
        with self.assertRaises(SettingsError):
            load_settings(self._env(**{INLINE_VAR: "-----BEGIN PRIVATE KEY-----\nnope\n"}))

    def test_the_key_is_not_in_the_settings_repr(self):
        settings = load_settings(self._env(**{INLINE_VAR: PEM.decode()}))
        text = repr(settings)
        self.assertNotIn("BEGIN", text)
        self.assertNotIn(PEM.decode().splitlines()[1], text)
        self.assertNotIn("s3cret", text)


class StorageRootOwnershipTests(unittest.TestCase):
    """The storage root belongs to the API process, and to nothing else.

    Everything under RELIUM_STORAGE_ROOT — webhook delivery claims, the
    verified-job store with its leases and retries, and the publication
    journal — is reached through RepositoryStorage, which is constructed in
    exactly one place: build_application, in the API. The lifecycle worker
    coordinates entirely through PostgreSQL.

    If that ever stops being true, a single Railway volume attached to the API
    is no longer sufficient and the deployment topology is wrong. These tests
    are here to fail on that day rather than let it be discovered by a
    duplicated GitHub publication.
    """

    def test_repository_storage_is_constructed_only_by_the_api(self):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-c",
             "import pathlib,re,sys;"
             "hits=[str(p) for p in pathlib.Path('agent').rglob('*.py')"
             " if 'RepositoryStorage(' in p.read_text(encoding='utf-8')];"
             "print('\\n'.join(sorted(hits)))"],
            capture_output=True, text=True, cwd=str(Path(__file__).parent))
        sites = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(
            [Path(s).as_posix() for s in sites],
            ["agent/github_app/server.py"],
            "RepositoryStorage is constructed outside the API; the storage "
            "root would then be shared state and one volume is not enough")

    def test_the_worker_does_not_require_a_storage_root(self):
        """The worker must start with no filesystem configuration at all."""
        from agent.worker.publisher_config import build_publisher_factory

        # No storage root anywhere in its configuration, and it still resolves.
        factory = build_publisher_factory({
            "RELIUM_GITHUB_APP_ID": "42", INLINE_VAR: PEM.decode()})
        self.assertIsNotNone(factory)

    def test_the_worker_module_never_imports_storage(self):
        source = Path("agent/worker/lifecycle_worker.py").read_text(encoding="utf-8")
        self.assertNotIn("RepositoryStorage", source)
        self.assertNotIn("RELIUM_STORAGE_ROOT", source)

    def test_the_worker_publisher_path_touches_no_filesystem(self):
        """The worker's publication journal is the database, not a directory."""
        for module in ("agent/metadata_evidence/publishers.py",
                       "agent/metadata_evidence/publication_reconcile.py",
                       "agent/metadata_evidence/change_request.py"):
            source = Path(module).read_text(encoding="utf-8")
            self.assertNotIn("RepositoryStorage", source, module)
            self.assertNotIn("storage_root", source, module)


if __name__ == "__main__":
    unittest.main()
