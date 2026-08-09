"""The worker's outbound publisher wiring.

This exists because the wiring was missing and nothing caught it: both
outbound handlers were registered and correct, `configure_publisher` existed,
and no production entrypoint ever called it. The handlers therefore ran, found
no publisher, and recorded that nothing was published — durable and honest,
and never delivered. A live run was the first thing to notice.

So these tests assert the wiring itself, not just the handlers.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent.worker.publisher_config import build_publisher_factory
from pem_test_support import test_private_key_pem, write_test_private_key


def _key_file():
    """A real PEM on disk. The key is now loaded, not just read."""
    directory = tempfile.mkdtemp(prefix="relium-worker-key-")
    return write_test_private_key(Path(directory) / "app.pem")


class ConfigurationTests(unittest.TestCase):
    def test_no_factory_without_app_configuration(self):
        """Unconfigured stays on the existing 'nothing attempted' path."""
        self.assertIsNone(build_publisher_factory({}))
        self.assertIsNone(build_publisher_factory({"RELIUM_GITHUB_APP_ID": "1"}))
        self.assertIsNone(build_publisher_factory(
            {"RELIUM_GITHUB_PRIVATE_KEY_PATH": "/tmp/x.pem"}))

    def test_a_missing_key_file_fails_loudly(self):
        """Silently degrading to 'nothing published' is what caused the gap."""
        with self.assertRaises(RuntimeError):
            build_publisher_factory({
                "RELIUM_GITHUB_APP_ID": "424242",
                "RELIUM_GITHUB_PRIVATE_KEY_PATH": "/definitely/not/here.pem",
            })

    def test_a_malformed_key_fails_loudly(self):
        directory = tempfile.mkdtemp(prefix="relium-worker-badkey-")
        bad = Path(directory) / "app.pem"
        bad.write_text("not a key", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            build_publisher_factory({
                "RELIUM_GITHUB_APP_ID": "424242",
                "RELIUM_GITHUB_PRIVATE_KEY_PATH": str(bad),
            })

    def test_a_configured_app_yields_a_factory(self):
        factory = build_publisher_factory({
            "RELIUM_GITHUB_APP_ID": "424242",
            "RELIUM_GITHUB_PRIVATE_KEY_PATH": _key_file(),
        })
        self.assertTrue(callable(factory))

    def test_an_inline_key_also_yields_a_factory(self):
        """The worker obeys the same precedence rule as the API."""
        factory = build_publisher_factory({
            "RELIUM_GITHUB_APP_ID": "424242",
            "RELIUM_GITHUB_PRIVATE_KEY": test_private_key_pem().decode(),
        })
        self.assertTrue(callable(factory))


class FactoryTests(unittest.TestCase):
    @property
    def ENV(self):
        return {
            "RELIUM_GITHUB_APP_ID": "424242",
            "RELIUM_GITHUB_PRIVATE_KEY_PATH": _key_file(),
            "RELIUM_GITHUB_INSTALLATION_ID": "777",
        }

    def _factory(self, env=None):
        return build_publisher_factory({**self.ENV, **(env or {})})

    def _patches(self, installation=None):
        return (
            mock.patch("agent.github_app.auth.create_app_jwt", return_value="JWT"),
            mock.patch("agent.github_app.auth.get_installation_token",
                       return_value="INSTALLATION_TOKEN"),
            mock.patch.object(
                __import__("agent.github_app.client", fromlist=["GitHubClient"]).GitHubClient,
                "get_repository_installation",
                return_value=installation or {"id": 999}),
        )

    def test_it_builds_a_publisher_for_the_tenant(self):
        factory = self._factory()
        with self._patches()[0], self._patches()[1], self._patches()[2]:
            publisher = factory(organization_id="acme", repository_id="analytics",
                                environment="production")
        self.assertEqual(publisher.owner, "acme")
        self.assertEqual(publisher.repository, "analytics")
        self.assertEqual(publisher.expected_app_id, 424242)
        # The publisher must be able to do the thing the worker needs.
        self.assertTrue(hasattr(publisher, "submit_request_changes"))
        self.assertTrue(hasattr(publisher, "publish_comment"))
        self.assertTrue(hasattr(publisher, "publish_check"))

    def test_the_installation_is_resolved_from_the_repository_when_unset(self):
        """A multi-tenant worker needs no per-tenant installation id."""
        factory = self._factory({"RELIUM_GITHUB_INSTALLATION_ID": ""})
        p0, p1, p2 = self._patches(installation={"id": 31337})
        with p0, p1, p2 as lookup:
            factory(organization_id="acme", repository_id="analytics",
                    environment="production")
        lookup.assert_called_once()
        self.assertEqual(lookup.call_args[0][:2], ("acme", "analytics"))

    def test_slack_is_attached_only_when_configured(self):
        p0, p1, p2 = self._patches()[:3]
        with p0, p1, p2:
            without = self._factory()(organization_id="acme",
                                      repository_id="analytics",
                                      environment="production")
        self.assertIsNone(without.slack_publisher)

        p0, p1, p2 = self._patches()[:3]
        with p0, p1, p2:
            with_slack = self._factory({
                "RELIUM_SLACK_WEBHOOK_URL": "https://hooks.example.invalid/x",
                "RELIUM_SLACK_NOTIFY_WARN": "true",
            })(organization_id="acme", repository_id="analytics",
               environment="production")
        self.assertIsNotNone(with_slack.slack_publisher)
        self.assertTrue(with_slack.slack_publisher.notify_warn)


class EntrypointTests(unittest.TestCase):
    def test_the_worker_entrypoint_installs_the_publisher(self):
        """The regression itself: main() must call configure_publisher."""
        import inspect

        from agent.worker import lifecycle_worker

        source = inspect.getsource(lifecycle_worker.main)
        self.assertIn("build_publisher_factory", source)
        self.assertIn("configure_publisher(factory)", source)

    def test_both_outbound_handlers_are_registered(self):
        from agent.metadata_evidence.change_request import (
            EVENT_TYPE as CHANGE_REQUEST_EVENT,
        )
        from agent.metadata_evidence.publication_reconcile import (
            EVENT_TYPE as PUBLICATION_EVENT,
        )
        from agent.worker.lifecycle_worker import registry

        self.assertIn(CHANGE_REQUEST_EVENT, registry.supported())
        self.assertIn(PUBLICATION_EVENT, registry.supported())


if __name__ == "__main__":
    unittest.main()
