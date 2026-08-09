"""Outbound surfaces for review republication.

One small interface with three methods, so the reconciler never holds a
GitHub client or a Slack webhook directly and can be tested without either.

``GitHubSlackPublisher`` is the real one. It updates in place when it holds an
identity and only creates when it does not, which is what keeps a
recomputation from leaving a second comment or a second check run behind.
"""
from __future__ import annotations

from agent.github_app.comments import upsert_review_comment


class GitHubSlackPublisher:
    """Publishes to a real repository and a real Slack sink.

    ``slack_publisher`` is the existing SlackPublicationSink, used unchanged.
    Its classification rules decide whether anything is sent; this class never
    second-guesses them and never sends on its behalf.
    """

    def __init__(self, client, *, owner, repository, expected_app_id,
                 slack_publisher=None, pull_url_template=None):
        self.client = client
        self.owner = owner
        self.repository = repository
        self.expected_app_id = expected_app_id
        self.slack_publisher = slack_publisher
        self.pull_url_template = (
            pull_url_template
            or "https://github.com/{owner}/{repository}/pull/{pull_number}")

    def publish_comment(self, *, pull_number, body, comment_id=None):
        """Update the sticky comment if we know it; otherwise find or create it."""
        if comment_id:
            # Editing by id is what makes reconciliation provable: the id in
            # the response must equal the id we already had.
            return self.client.update_issue_comment(
                self.owner, self.repository, int(comment_id), body)
        return upsert_review_comment(
            self.client, owner=self.owner, repository=self.repository,
            pull_number=pull_number, body=body,
            expected_app_id=self.expected_app_id)

    def publish_check(self, *, head_sha, payload, check_run_id=None):
        """PATCH the existing run, or create the first one."""
        if check_run_id:
            # head_sha is immutable on a check run and GitHub rejects it on
            # update, so the update payload carries only what may change.
            update = {k: v for k, v in payload.items() if k != "head_sha"}
            return self.client.update_check_run(
                self.owner, self.repository, int(check_run_id), update)
        return self.client.create_check_run(self.owner, self.repository, payload)

    def submit_request_changes(self, *, pull_number, body):
        """Submit a real GitHub pull-request review requesting changes.

        Uses `pull_requests: write`, already in the App's enforced permission
        set. Any GitHub error propagates: the caller records the failure
        rather than reporting a success that did not happen.
        """
        return self.client.create_pull_request_review(
            self.owner, self.repository, pull_number,
            body=body, event="REQUEST_CHANGES")

    def publish_slack(self, *, publication_id, pull_number, result):
        if self.slack_publisher is None:
            return {"state": "disabled", "publication_id": publication_id}
        pull_url = self.pull_url_template.format(
            owner=self.owner, repository=self.repository, pull_number=pull_number)
        return self.slack_publisher.publish(
            publication_id=publication_id,
            repository=f"{self.owner}/{self.repository}",
            pull_number=pull_number, result=result, pull_url=pull_url)


class NullPublisher:
    """Publishes nowhere, and says so.

    Used when a deployment has no GitHub credentials configured. Every method
    reports an explicit disabled state rather than a success, so an
    unpublished decision is never mistaken for a delivered one.
    """

    def publish_comment(self, *, pull_number, body, comment_id=None):
        return {"id": comment_id, "state": "disabled"}

    def publish_check(self, *, head_sha, payload, check_run_id=None):
        return {"id": check_run_id, "state": "disabled"}

    def submit_request_changes(self, *, pull_number, body):
        raise RuntimeError(
            "no GitHub credentials are configured, so a request-changes "
            "review cannot be submitted")

    def publish_slack(self, *, publication_id, pull_number, result):
        return {"state": "disabled", "publication_id": publication_id}
