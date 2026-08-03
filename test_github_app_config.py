import tempfile
import unittest
from pathlib import Path


class GitHubAppConfigTests(unittest.TestCase):
    def test_missing_config_uses_safe_defaults(self):
        from agent.github_app.config import load_repository_config

        config = load_repository_config(None)

        self.assertEqual(config.version, 1)
        self.assertTrue(config.enabled)
        self.assertEqual(config.manifest_path, "target/manifest.json")
        self.assertEqual(config.mode, "warn")
        self.assertEqual(config.enforcement_mode, "shadow")

    def test_loads_repository_yaml(self):
        from agent.github_app.config import load_repository_config

        config = load_repository_config(
            b"version: 1\nenabled: false\nmanifest_path: artifacts/manifest.json\n"
        )

        self.assertEqual(config.version, 1)
        self.assertFalse(config.enabled)
        self.assertEqual(config.manifest_path, "artifacts/manifest.json")

    def test_loads_explicit_enforcement_mode(self):
        from agent.github_app.config import load_repository_config

        config = load_repository_config(
            "mode: warn\nenforcement_mode: enforce\n"
        )

        self.assertEqual(config.mode, "warn")
        self.assertEqual(config.enforcement_mode, "enforce")

    def test_partial_evidence_policy_overrides_preserve_default_requirements(self):
        from agent.github_app.config import load_repository_config

        config = load_repository_config(
            "evidence_policy:\n"
            "  version: team-v2\n"
            "  sources:\n"
            "    slack: disabled\n"
        )

        self.assertEqual(config.evidence_policy.version, "team-v2")
        self.assertEqual(config.evidence_policy.requirements["slack"].value, "disabled")
        self.assertEqual(config.evidence_policy.requirements["head_manifest"].value, "required")

    def test_legacy_block_mode_does_not_enable_enforcement(self):
        from agent.github_app.config import load_repository_config

        config = load_repository_config("mode: block\n")

        self.assertEqual(config.mode, "block")
        self.assertEqual(config.enforcement_mode, "shadow")

    def test_rejects_unknown_versions_and_invalid_types(self):
        from agent.github_app.config import RepositoryConfigError, load_repository_config

        with self.assertRaisesRegex(RepositoryConfigError, "version"):
            load_repository_config("version: 2\n")
        with self.assertRaisesRegex(RepositoryConfigError, "enabled"):
            load_repository_config("version: 1\nenabled: yes\n")
        with self.assertRaisesRegex(RepositoryConfigError, "object"):
            load_repository_config("- version\n- 1\n")
        with self.assertRaisesRegex(RepositoryConfigError, "mode"):
            load_repository_config("mode: enforce\n")
        with self.assertRaisesRegex(RepositoryConfigError, "enforcement_mode"):
            load_repository_config("enforcement_mode: block\n")

    def test_rejects_absolute_traversal_and_windows_paths(self):
        from agent.github_app.config import RepositoryConfigError, load_repository_config

        unsafe_paths = (
            "/etc/passwd",
            "../manifest.json",
            "target/../../manifest.json",
            r"C:\secrets\manifest.json",
            r"target\manifest.json",
        )
        for unsafe_path in unsafe_paths:
            with self.subTest(path=unsafe_path):
                with self.assertRaisesRegex(RepositoryConfigError, "manifest_path"):
                    load_repository_config(
                        f'version: 1\nmanifest_path: "{unsafe_path}"\n'
                    )

    def test_resolves_paths_only_within_repository_root(self):
        from agent.github_app.config import RepositoryConfigError, resolve_repository_path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repository"
            root.mkdir()
            resolved = resolve_repository_path(root, "target/manifest.json")
            expected = (root / "target" / "manifest.json").resolve()
            self.assertEqual(resolved, expected)

            with self.assertRaisesRegex(RepositoryConfigError, "repository"):
                resolve_repository_path(root, "../outside.json")

    def test_existing_symlink_cannot_escape_repository_root(self):
        from agent.github_app.config import RepositoryConfigError, resolve_repository_path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repository"
            outside = Path(tmp) / "outside"
            root.mkdir()
            outside.mkdir()
            link = root / "target"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("Symlinks are not available in this Windows environment")

            with self.assertRaisesRegex(RepositoryConfigError, "repository"):
                resolve_repository_path(root, "target/manifest.json")


if __name__ == "__main__":
    unittest.main()
