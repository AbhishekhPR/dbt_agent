import json

from agent.deployment_review_service import review_manifest_change
from agent.github_app.checks import CHECK_NAME, create_review_check
from agent.github_app.client import GitHubNotFoundError
from agent.github_app.comments import upsert_review_comment
from agent.github_app.config import DEFAULT_MANIFEST_PATH, load_repository_config
from agent.github_app.review_comment import render_review_comment


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
            if config.manifest_path == DEFAULT_MANIFEST_PATH:
                message = (
                    "Relium could not find target/manifest.json. "
                    "Run dbt compile before the Relium review."
                )
            else:
                message = (
                    f"Relium could not find {config.manifest_path}. "
                    "Generate the configured dbt manifest before the Relium review."
                )
            return self._publish_neutral(
                event,
                client,
                config.enforcement_mode,
                status="missing_manifest",
                message=message,
                expected_app_id=expected_app_id,
            )
        try:
            manifest = json.loads(manifest_content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewRunnerError("Manifest must contain valid UTF-8 JSON.") from exc
        if not isinstance(manifest, dict):
            raise ReviewRunnerError("Manifest must be a JSON object.")

        try:
            base_manifest_content = client.get_file(
                owner, repository, config.manifest_path, event.base_sha
            )
        except GitHubNotFoundError:
            base_manifest_content = None
        previous_manifest = None
        if base_manifest_content is not None:
            try:
                previous_manifest = json.loads(base_manifest_content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReviewRunnerError("Base manifest must contain valid UTF-8 JSON.") from exc
            if not isinstance(previous_manifest, dict):
                raise ReviewRunnerError("Base manifest must be a JSON object.")

        changed_files = client.compare_files(owner, repository, event.base_sha, event.head_sha)
        if not getattr(changed_files, "complete", True):
            return self._publish_neutral(
                event,
                client,
                config.enforcement_mode,
                status="large_pr",
                message=(
                    "Relium could not safely enumerate every changed file in this pull "
                    "request. The review was skipped because GitHub may have truncated "
                    "the compare response."
                ),
                expected_app_id=expected_app_id,
            )
        try:
            result = self.reviewer(
                manifest=manifest,
                previous_manifest=previous_manifest,
                changed_files=changed_files,
                deployment_id=f"github:{event.repository.id}:{event.head_sha}",
                manifest_source={
                    "base": "github" if previous_manifest is not None else "unavailable",
                    "head": "github",
                },
                base_sha=event.base_sha,
                head_sha=event.head_sha,
            )
        except ValueError as exc:
            if str(exc) != "At least one changed model is required.":
                raise
            return self._publish_neutral(
                event,
                client,
                config.enforcement_mode,
                status="skipped",
                message="Relium skipped analysis because this pull request changes no dbt models.",
                expected_app_id=expected_app_id,
            )
        return self._publish(
            event,
            client,
            config.enforcement_mode,
            result,
            status="reviewed",
            expected_app_id=expected_app_id,
        )

    def _publish_neutral(
        self,
        event,
        client,
        enforcement_mode,
        *,
        status,
        message,
        expected_app_id,
    ):
        result = {"decision": "SKIPPED", "rendered": {"markdown": f"## Relium deployment review\n\n{message}"}}
        return self._publish(
            event,
            client,
            enforcement_mode,
            result,
            status=status,
            expected_app_id=expected_app_id,
        )

    def _publish(
        self,
        event,
        client,
        enforcement_mode,
        result,
        *,
        status,
        expected_app_id,
    ):
        owner = event.repository.owner
        repository = event.repository.name
        comment_body = render_review_comment(result)
        check_result = dict(result)
        check_result["rendered"] = {
            **dict(result.get("rendered") or {}),
            "markdown": comment_body,
        }
        publication_id = f"review-{event.repository.id}-{event.head_sha}-{enforcement_mode}"
        journal = self.storage.get_publication_journal(
            event.repository.id,
            publication_id,
        )
        comment_entry = journal.get("comment")
        if comment_entry and comment_entry.get("state") == "complete":
            comment = comment_entry.get("value") or {
                "id": comment_entry.get("id"),
                "body": comment_body,
            }
        else:
            if not comment_entry:
                self.storage.record_publication_step(
                    event.repository.id,
                    publication_id,
                    "comment",
                    {"state": "started"},
                )
            comment = upsert_review_comment(
                client,
                owner=owner,
                repository=repository,
                pull_number=event.pull_number,
                body=comment_body,
                expected_app_id=expected_app_id,
            )
            self.storage.record_publication_step(
                event.repository.id,
                publication_id,
                "comment",
                {"state": "complete", "value": comment},
            )

        check_entry = journal.get("check")
        if check_entry and check_entry.get("state") == "complete":
            check = check_entry.get("value") or {"id": check_entry.get("id")}
        else:
            if not check_entry:
                self.storage.record_publication_step(
                    event.repository.id,
                    publication_id,
                    "check",
                    {"state": "started"},
                )
            check = self._reconcile_check(
                client,
                owner=owner,
                repository=repository,
                head_sha=event.head_sha,
                publication_id=publication_id,
                result=check_result,
                enforcement_mode=enforcement_mode,
                reconcile=bool(check_entry),
            )
            self.storage.record_publication_step(
                event.repository.id,
                publication_id,
                "check",
                {"state": "complete", "value": check},
            )
        return {"status": status, "result": result, "comment": comment, "check": check}

    @staticmethod
    def _reconcile_check(
        client,
        *,
        owner,
        repository,
        head_sha,
        publication_id,
        result,
        enforcement_mode,
        reconcile,
    ):
        list_runs = getattr(client, "list_check_runs", None)
        if reconcile and list_runs is not None:
            for check in list_runs(
                owner,
                repository,
                head_sha=head_sha,
                check_name=CHECK_NAME,
            ):
                if (
                    isinstance(check, dict)
                    and check.get("name") == CHECK_NAME
                    and str(check.get("head_sha", "")) == str(head_sha)
                    and str(check.get("external_id", "")) == publication_id
                ):
                    return check
        return create_review_check(
            client,
            owner=owner,
            repository=repository,
            head_sha=head_sha,
            result=result,
            enforcement_mode=enforcement_mode,
            external_id=publication_id,
        )
