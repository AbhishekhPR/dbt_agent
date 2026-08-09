"""Does the pilot's durable state actually survive a restart?

A volume that exists is not the same as state that survives, and "the
directory is still there" is not a proof of anything. These tests cross a real
restart boundary: the storage object and the served application are destroyed
and rebuilt over the same root, which is what a Railway redeploy does to a
container with a mounted volume.

What must survive:

  * a webhook delivery claim, so GitHub redelivering an event it already
    delivered does not produce a second review and a second published comment;
  * the verified-job record, so work accepted before the restart is picked up
    after it rather than lost;
  * the publication journal, so a comment or check already posted is not
    posted again.

Everything here is filesystem-only and needs no database, because the state
under test is filesystem-only. That is precisely why the single-replica
limitation exists and is documented.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from agent.github_app.http_app import create_http_app
from agent.github_app.jobs import WebhookJob
from agent.github_app.storage import RepositoryStorage

SECRET = "pilot-restart-secret"
REPOSITORY_ID = 987654


def _signed(body: bytes) -> str:
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _pull_request_body(pull_number=41):
    return json.dumps({
        "action": "opened",
        "number": pull_number,
        "pull_request": {
            "number": pull_number,
            "head": {"sha": "b" * 40, "ref": "feature"},
            "base": {"sha": "a" * 40, "ref": "main"},
            "draft": False,
            "state": "open",
        },
        "repository": {
            "id": REPOSITORY_ID,
            "name": "analytics",
            "full_name": "acme/analytics",
            "owner": {"login": "acme"},
            "default_branch": "main",
        },
        "installation": {"id": 5150},
        "sender": {"login": "octocat"},
    }).encode()


class _StubQueue:
    """Accepts work without running it, so the test observes storage, not GitHub."""

    is_running = False

    def __init__(self):
        self.enqueued = []

    def start(self):
        self.is_running = True

    def stop(self, timeout=None):
        self.is_running = False

    def enqueue(self, job):
        self.enqueued.append(job)
        return True


class WebhookRedeliveryAcrossRestartTests(unittest.TestCase):
    """The API is stopped and rebuilt over the same volume."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="relium-volume-"))
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))

    def _serve(self):
        """One 'container': a fresh storage object and app over the same root."""
        storage = RepositoryStorage(self.root)
        queue = _StubQueue()
        app = create_http_app(
            webhook_secret=SECRET, job_queue=queue, max_body_bytes=1024 * 1024,
            shutdown_timeout_seconds=1.0, clock=lambda: 0.0, job_store=storage)
        return storage, queue, app

    def _deliver(self, app, delivery_id, body):
        with TestClient(app) as client:
            return client.post(
                "/github/webhook", content=body,
                headers={"X-GitHub-Delivery": delivery_id,
                         "X-GitHub-Event": "pull_request",
                         "X-Hub-Signature-256": _signed(body),
                         "Content-Type": "application/json"})

    def test_a_redelivery_after_restart_is_refused(self):
        body = _pull_request_body()
        delivery = "11111111-2222-3333-4444-555555555555"

        _storage, queue_before, app_before = self._serve()
        first = self._deliver(app_before, delivery, body)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json()["status"], "accepted")
        self.assertEqual(len(queue_before.enqueued), 1)

        # --- restart boundary: nothing in memory survives, the volume does ---
        del _storage, app_before

        _storage_after, queue_after, app_after = self._serve()
        second = self._deliver(app_after, delivery, body)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(
            second.json()["status"], "duplicate",
            "GitHub redelivered an event the previous container already "
            "accepted, and it was accepted a second time")
        self.assertEqual(
            queue_after.enqueued, [],
            "a redelivered event was queued for processing again")

    def test_a_different_delivery_after_restart_is_still_accepted(self):
        """The claim must be per delivery, not a blanket 'seen this repo'."""
        body = _pull_request_body()
        _s1, _q1, app_before = self._serve()
        self.assertEqual(
            self._deliver(app_before, "aaaaaaaa-0000-0000-0000-000000000001",
                          body).json()["status"], "accepted")

        _s2, queue_after, app_after = self._serve()
        second = self._deliver(app_after, "aaaaaaaa-0000-0000-0000-000000000002",
                               body)
        self.assertEqual(second.json()["status"], "accepted")
        self.assertEqual(len(queue_after.enqueued), 1)

    def test_the_volume_is_what_carries_the_claim(self):
        """Same code, empty volume: the redelivery is accepted again.

        This is the control. Without it, the test above could pass because of
        something other than the filesystem, and the whole persistence
        argument would be unproven.
        """
        body = _pull_request_body()
        delivery = "cccccccc-0000-0000-0000-000000000003"
        _s1, _q1, app_before = self._serve()
        self.assertEqual(self._deliver(app_before, delivery, body).json()["status"],
                         "accepted")

        shutil.rmtree(self.root, ignore_errors=True)      # volume lost
        self.root.mkdir(parents=True, exist_ok=True)

        _s2, queue_after, app_after = self._serve()
        self.assertEqual(
            self._deliver(app_after, delivery, body).json()["status"], "accepted",
            "with the volume gone the claim must be gone too; if this still "
            "said 'duplicate' the dedupe would not be coming from the volume")
        self.assertEqual(len(queue_after.enqueued), 1)


class JobAndJournalSurviveRestartTests(unittest.TestCase):
    """Storage-level state, across a restart of the storage object."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="relium-volume-"))
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))

    def _job(self, delivery_id="dddddddd-0000-0000-0000-000000000004"):
        return WebhookJob(delivery_id=delivery_id, event_name="pull_request",
                          raw_body=_pull_request_body(), received_at=0.0,
                          repository_id=REPOSITORY_ID)

    def test_an_unfinished_job_is_recovered_after_a_restart(self):
        job = self._job()
        before = RepositoryStorage(self.root)
        self.assertTrue(before.persist_verified_job(REPOSITORY_ID, job))
        before.claim_job(REPOSITORY_ID, job.delivery_id, owner="container-1",
                         now=0.0, lease_seconds=30.0)
        before.mark_processing(REPOSITORY_ID, job.delivery_id, owner="container-1")
        del before                                   # container dies mid-job

        after = RepositoryStorage(self.root)
        recovered = after.recover_all_jobs(now=1000.0)   # lease long expired
        self.assertEqual(
            [j.delivery_id for j in recovered], [job.delivery_id],
            "work accepted before the restart was lost")

    def test_a_completed_job_is_not_resurrected(self):
        job = self._job("eeeeeeee-0000-0000-0000-000000000005")
        before = RepositoryStorage(self.root)
        before.persist_verified_job(REPOSITORY_ID, job)
        before.claim_job(REPOSITORY_ID, job.delivery_id, owner="container-1",
                         now=0.0, lease_seconds=30.0)
        before.complete_job(REPOSITORY_ID, job.delivery_id)
        del before

        after = RepositoryStorage(self.root)
        self.assertEqual(
            after.recover_all_jobs(now=1000.0), [],
            "a finished job was re-queued after restart, which would publish twice")

    def test_the_publication_journal_survives_and_still_deduplicates(self):
        publication = "pub-restart-1"
        before = RepositoryStorage(self.root)
        self.assertTrue(before.claim_publication_step(
            REPOSITORY_ID, publication, "comment", "comment-9001"))
        del before

        after = RepositoryStorage(self.root)
        journal = after.get_publication_journal(REPOSITORY_ID, publication)
        self.assertEqual(journal.get("comment"), "comment-9001",
                         "the publication journal did not survive the restart")
        self.assertFalse(
            after.claim_publication_step(
                REPOSITORY_ID, publication, "comment", "comment-9002"),
            "a second container re-claimed a step already published, which is "
            "how a pull request gets two Relium comments")

    def test_a_delivery_claim_survives_the_storage_object(self):
        delivery = "ffffffff-0000-0000-0000-000000000006"
        before = RepositoryStorage(self.root)
        self.assertTrue(before.claim_delivery(REPOSITORY_ID, delivery))
        before.complete_delivery(REPOSITORY_ID, delivery)
        del before

        after = RepositoryStorage(self.root)
        self.assertFalse(after.claim_delivery(REPOSITORY_ID, delivery))


if __name__ == "__main__":
    unittest.main()
