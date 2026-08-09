"""Focused contract tests for the local live-E2E recording transport."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent.github_app.client import GitHubClient
from scripts.live_e2e.recording import RecordingGitHubTransport


class RecordingGitHubTransportTests(unittest.TestCase):
    def test_pull_request_reviews_receive_a_stable_valid_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = RecordingGitHubTransport(Path(tmp) / "github.json")
            client = GitHubClient("token", transport=transport)

            first = client.create_pull_request_review(
                "acme", "analytics", 42, body="Please fix this.",
                event="REQUEST_CHANGES")
            second = client.create_pull_request_review(
                "acme", "analytics", 42, body="Please fix this.",
                event="REQUEST_CHANGES")

        self.assertIsInstance(first["id"], int)
        self.assertGreater(first["id"], 0)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["state"], "CHANGES_REQUESTED")
        self.assertEqual(second["state"], "CHANGES_REQUESTED")
        review_calls = [
            call for call in transport.calls
            if call["method"] == "POST" and call["path"].endswith("/reviews")
        ]
        self.assertEqual(len(review_calls), 2)
        self.assertTrue(all(call["status"] == 201 for call in review_calls))


if __name__ == "__main__":
    unittest.main()
