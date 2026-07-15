import json

from agent.deployment_review_service import review_manifest_change
from agent.github_app.checks import create_review_check
from agent.github_app.client import GitHubNotFoundError
from agent.github_app.comments import upsert_review_comment
from agent.github_app.config import load_repository_config


class ReviewRunnerError(ValueError):
    pass


class PullRequestReviewRunner:
    def __init__(self, *, storage, reviewer=review_manifest_change):
        self.storage = storage
        self.reviewer = reviewer

    def run(self, event, client, *, expected_app_id):
        if not self.storage.claim_delivery(event.repository.id, event.delivery_id):
            return {"status": "duplicate", "delivery_id": event.delivery_id}
        try:
            response = self._run_claimed(event, client, expected_app_id=expected_app_id)
        except Exception:
            self.storage.release_delivery(event.repository.id, event.delivery_id)
            raise
        if response["status"] == "disabled":
            self.storage.release_delivery(event.repository.id, event.delivery_id)
        else:
            self.storage.complete_delivery(event.repository.id, event.delivery_id)
        return response

    def _run_claimed(self, event, client, *, expected_app_id):
        owner = event.repository.owner
        repository = event.repository.name
        try:
            config_content = client.get_file(owner, repository, "relium.yml", event.head_sha)
        except GitHubNotFoundError:
            config_content = None
        config = load_repository_config(config_content)
        if not config.enabled:
            return {"status": "disabled", "delivery_id": event.delivery_id}

        try:
            manifest_content = client.get_file(
                owner, repository, config.manifest_path, event.head_sha
            )
        except GitHubNotFoundError:
            manifest_content = None
        if manifest_content is None:
            return self._publish_neutral(
                event,
                client,
                config.mode,
                status="missing_manifest",
                message=(
                    f"Relium could not find {config.manifest_path}. "
                    "Run dbt compile before the Relium review."
                ),
                expected_app_id=expected_app_id,
            )
        try:
            manifest = json.loads(manifest_content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewRunnerError("Manifest must contain valid UTF-8 JSON.") from exc
        if not isinstance(manifest, dict):
            raise ReviewRunnerError("Manifest must be a JSON object.")

        changed_files = client.compare_files(owner, repository, event.base_sha, event.head_sha)
        try:
            result = self.reviewer(
                manifest=manifest,
                changed_files=changed_files,
                deployment_id=f"github:{event.repository.id}:{event.head_sha}",
            )
        except ValueError as exc:
            if str(exc) != "At least one changed model is required.":
                raise
            return self._publish_neutral(
                event,
                client,
                config.mode,
                status="skipped",
                message="Relium skipped analysis because this pull request changes no dbt models.",
                expected_app_id=expected_app_id,
            )
        return self._publish(
            event,
            client,
            config.mode,
            result,
            status="reviewed",
            expected_app_id=expected_app_id,
        )

    def _publish_neutral(
        self, event, client, mode, *, status, message, expected_app_id
    ):
        result = {"decision": "SKIPPED", "rendered": {"markdown": f"## Relium deployment review\n\n{message}"}}
        return self._publish(
            event,
            client,
            mode,
            result,
            status=status,
            expected_app_id=expected_app_id,
        )

    def _publish(self, event, client, mode, result, *, status, expected_app_id):
        owner = event.repository.owner
        repository = event.repository.name
        comment = upsert_review_comment(
            client,
            owner=owner,
            repository=repository,
            pull_number=event.pull_number,
            body=result["rendered"]["markdown"],
            expected_app_id=expected_app_id,
        )
        check = create_review_check(
            client,
            owner=owner,
            repository=repository,
            head_sha=event.head_sha,
            result=result,
            mode=mode,
        )
        return {"status": status, "result": result, "comment": comment, "check": check}
