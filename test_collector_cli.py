from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from agent.cli import cli
from agent.collector.runner import CollectionOutcome


class CollectorCliTests(unittest.TestCase):
    def _env(self):
        return {
            "RELIUM_API_URL": "https://relium.test",
            "RELIUM_API_TOKEN": "rlm_abc.secret-value",
            "RELIUM_WAREHOUSE_DSN": "postgresql://user:secret@warehouse/db",
            "RELIUM_ENVIRONMENT": "production",
        }

    def test_request_id_is_forwarded_to_the_collection_runner(self):
        with patch.dict(os.environ, self._env(), clear=False), patch(
            "agent.collector.run_collection",
            return_value=CollectionOutcome(ok=True, reason="ok"),
        ) as run:
            result = CliRunner().invoke(
                cli, ["collect", "--request-id", "req-exact", "--json"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(run.call_args.kwargs["request_id"], "req-exact")
        self.assertFalse(run.call_args.kwargs["verify_only"])

    def test_test_flag_requests_real_warehouse_verification(self):
        with patch.dict(os.environ, self._env(), clear=False), patch(
            "agent.collector.run_collection",
            return_value=CollectionOutcome(ok=True, reason="collector verified"),
        ) as run:
            result = CliRunner().invoke(cli, ["collect", "--test", "--json"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(run.call_args.kwargs["verify_only"])
        self.assertIsNone(run.call_args.kwargs["request_id"])


if __name__ == "__main__":
    unittest.main()
