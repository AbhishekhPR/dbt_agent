import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from bash_test_support import run_bash


ROOT = Path(__file__).resolve().parent


class GitHubAppPilotAssetTests(unittest.TestCase):
    def test_environment_template_contains_only_expected_placeholders(self):
        expected = [
            "RELIUM_GITHUB_APP_ID=",
            "RELIUM_GITHUB_WEBHOOK_SECRET=",
            "RELIUM_GITHUB_PRIVATE_KEY_PATH=",
            "RELIUM_STORAGE_ROOT=.relium/github-app",
            "RELIUM_WORKER_COUNT=2",
            "RELIUM_QUEUE_CAPACITY=100",
            "RELIUM_MAX_RETRIES=3",
            "RELIUM_RETRY_BASE_SECONDS=1",
            "RELIUM_HOST=127.0.0.1",
            "RELIUM_PORT=8000",
        ]
        lines = (ROOT / ".env.github-app.example").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(lines, expected)

    def test_gitignore_protects_pilot_credentials_and_storage(self):
        candidates = "\n".join(
            (
                ".env",
                ".env.github-app",
                "pilot.pem",
                "pilot.private-key.pem",
                ".relium/github-app/delivery",
                "github-app-storage/delivery",
            )
        )
        result = subprocess.run(
            ["git", "check-ignore", "--stdin"],
            cwd=ROOT,
            input=candidates.encode("utf-8"),
            capture_output=True,
            check=True,
        )
        output_lines = set(result.stdout.decode("utf-8").splitlines())
        expected_lines = set(candidates.splitlines())
        self.assertEqual(output_lines, expected_lines)
        example = subprocess.run(
            ["git", "check-ignore", ".env.github-app.example"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(example.returncode, 1)

    def test_scripts_are_strict_and_use_existing_server_interfaces(self):
        scripts = {
            "github_app_pilot_start.sh": "-m agent.github_app.server",
            "github_app_pilot_health.sh": "/healthz",
            "github_app_pilot_preflight.sh": "RELIUM_GITHUB_WEBHOOK_SECRET",
        }
        for name, required in scripts.items():
            with self.subTest(name=name):
                path = ROOT / "scripts" / name
                content = path.read_text(encoding="utf-8")
                self.assertTrue(content.startswith("#!/usr/bin/env bash\n"))
                self.assertIn("set -euo pipefail", content)
                self.assertIn(required, content)
                syntax = run_bash(["-n"], input_text=content)
                self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_preflight_accepts_fake_local_credentials_without_printing_them(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            bash_root = root.relative_to(ROOT)
            key_path = root / "pilot-test.private-key.pem"
            key_value = "temporary-test-key-material"
            key_path.write_text(key_value, encoding="utf-8")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            environment = os.environ.copy()
            environment.update(
                {
                    "RELIUM_GITHUB_APP_ID": "123",
                    "RELIUM_GITHUB_WEBHOOK_SECRET": "temporary-webhook-secret",
                    "RELIUM_GITHUB_PRIVATE_KEY_PATH": (
                        bash_root / key_path.name
                    ).as_posix(),
                    "RELIUM_STORAGE_ROOT": (bash_root / "storage").as_posix(),
                    "RELIUM_HOST": "127.0.0.1",
                    "RELIUM_PORT": str(port),
                }
            )
            result = run_bash(
                ["scripts/github_app_pilot_preflight.sh"],
                cwd=ROOT,
                env=environment,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        combined = result.stdout + result.stderr
        self.assertNotIn(environment["RELIUM_GITHUB_WEBHOOK_SECRET"], combined)
        self.assertNotIn(key_value, combined)
        self.assertNotIn(environment["RELIUM_GITHUB_PRIVATE_KEY_PATH"], combined)

    def test_live_pilot_guide_covers_commands_permissions_and_scenarios(self):
        guide = (ROOT / "docs" / "github-app-live-pilot.md").read_text(
            encoding="utf-8"
        )
        required = (
            "Relium Pilot",
            "https://www.relium.dev",
            "source .venv/bin/activate",
            "source .env.github-app",
            "python -m agent.github_app.server",
            "curl --fail http://127.0.0.1:8000/healthz",
            "/github/webhook",
            "Contents: Read",
            "Issues: Read and write",
            "Pull requests: Read",
            "Checks: Read and write",
            "Metadata: Read",
            "Valid PR with changed dbt model",
            "Missing manifest",
            "No changed dbt model",
            "Re-delivered webhook",
            "BLOCK result in warn mode",
            "BLOCK result in block mode",
        )
        for text in required:
            with self.subTest(text=text):
                self.assertIn(text, guide)

    def test_fixture_is_non_secret_and_contains_two_manifest_states(self):
        fixture = ROOT / "demo" / "github_app_pilot"
        config = yaml.safe_load((fixture / "relium.yml").read_text(encoding="utf-8"))
        self.assertEqual(
            config,
            {
                "manifest_path": "target/manifest.json",
                "mode": "warn",
                "enforcement_mode": "shadow",
                "enabled": True,
            },
        )
        previous = json.loads(
            (fixture / "previous_manifest.json").read_text(encoding="utf-8")
        )
        current = json.loads(
            (fixture / "current_manifest.json").read_text(encoding="utf-8")
        )
        self.assertIn("nodes", previous)
        self.assertIn("nodes", current)
        self.assertNotEqual(previous, current)
        for name in ("safe_customer_dimension.sql", "risky_revenue_refunds.sql"):
            self.assertTrue((fixture / "models" / name).is_file())
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in fixture.rglob("*")
            if path.is_file()
        )
        self.assertNotIn("BEGIN PRIVATE KEY", combined)
        self.assertNotIn("warehouse_password", combined)


if __name__ == "__main__":
    unittest.main()
