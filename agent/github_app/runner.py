import json

from agent.deployment_review_service import (
    lifecycle_code_findings,
    review_manifest_change,
    semantic_evidence_from_incident,
)
from agent.evidence_policy import EvidenceState, evaluate_evidence_policy
from agent.github_app.checks import CHECK_NAME, create_review_check
from agent.github_app.client import GitHubNotFoundError
from agent.github_app.comments import upsert_review_comment
from agent.github_app.config import DEFAULT_MANIFEST_PATH, load_repository_config
from agent.github_app.review_comment import render_review_comment
from agent.metadata_evidence.service import DisabledReviewLifecycle
from agent.metadata_evidence.waiting_publication import (
    render_manifest_waiting_result,
    render_waiting_result,
)


class ReviewRunnerError(ValueError):
    pass


class PullRequestReviewRunner:
    def __init__(
        self,
        *,
        storage,
        reviewer=review_manifest_change,
        slack_publisher=None,
        lifecycle=None,
        merge_blocking_allowed=None,
    ):
        self.storage = storage
        self.reviewer = reviewer
        self.slack_publisher = slack_publisher
        # ###############################################################
        # # relium.yml BELONGS TO THE CUSTOMER. THIS DOES NOT.          #
        # ###############################################################
        #
        # `enforcement_mode` is read out of relium.yml in the repository being
        # reviewed, so a Free workspace can write `enforcement_mode: enforce`
        # and, without this, would get Pro's release gate for nothing. The
        # dashboard refuses to SET enforce below Pro, but the dashboard is not
        # the only way that value arrives.
        #
        # A callable `(owner, repository) -> bool`, or None on a deployment
        # with no Polar configuration, where nothing is metered and the
        # customer's own file is the only authority — which is the behavior
        # every existing deployment already has.
        self._merge_blocking_allowed = merge_blocking_allowed
        # The review lifecycle is an explicit dependency. The runner never
        # opens a database connection itself; it holds this or it holds the
        # inert compatibility object.
        self.lifecycle = lifecycle or DisabledReviewLifecycle()

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

    def _apply_entitlements(self, config, owner, repository):
        """Cap the repository's configuration by what its workspace bought.

        Today that is one field. `enforce` becomes `shadow` below Pro, which
        changes the GitHub check CONCLUSION from failure to neutral — and
        nothing else. The review still runs, the decision is still computed the
        same way, and a BLOCK is still reported as a BLOCK: Free and Starter
        get the analysis and the recommendation, Pro gets the gate. Degrading
        the decision itself, rather than its enforcement, is the line this must
        never cross.
        """
        import dataclasses

        if self._merge_blocking_allowed is None:
            return config
        if config.enforcement_mode != "enforce":
            return config
        try:
            allowed = self._merge_blocking_allowed(owner, repository)
        except Exception:
            # A billing lookup that fails must not fail the review. Falling
            # back to `shadow` withholds a paid gate for one delivery; falling
            # back to `enforce` would hand out the paid gate to everyone the
            # moment the database hiccups.
            allowed = False
        if allowed:
            return config
        return dataclasses.replace(config, enforcement_mode="shadow")

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
        config = self._apply_entitlements(config, owner, repository)

        manifest = None
        try:
            manifest_content = client.get_file(
                owner, repository, config.manifest_path, event.head_sha
            )
        except GitHubNotFoundError:
            manifest_content = None
        if manifest_content is None and getattr(self.lifecycle, "enabled", False):
            evidence = self.lifecycle.get_manifest_evidence(
                organization_id=str(owner), repository_id=str(repository),
                commit_sha=event.head_sha)
            if evidence is not None:
                manifest = evidence["manifest"]
        if manifest_content is None:
            if getattr(self.lifecycle, "enabled", False):
                try:
                    base_content = client.get_file(
                        owner, repository, config.manifest_path, event.base_sha)
                except GitHubNotFoundError:
                    base_content = None
                base_manifest = None
                if base_content is not None:
                    try:
                        base_manifest = json.loads(base_content.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ReviewRunnerError(
                            "Base manifest must contain valid UTF-8 JSON.") from exc
                    if not isinstance(base_manifest, dict):
                        raise ReviewRunnerError("Base manifest must be a JSON object.")
                else:
                    base_evidence = self.lifecycle.get_manifest_evidence(
                        organization_id=str(owner), repository_id=str(repository),
                        commit_sha=event.base_sha)
                    if base_evidence is not None:
                        base_manifest = base_evidence["manifest"]
                # A CI-provided HEAD starts hosted mode. In that mode BASE is
                # equally mandatory: never turn a missing BASE into a silent
                # ``previous_manifest=None`` comparison.
                if manifest is None or base_manifest is None:
                    return self._wait_for_manifest(
                        event, client, config, base_manifest=base_manifest,
                        head_manifest=manifest,
                        expected_app_id=expected_app_id)
            else:
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
                    evidence_policy=config.evidence_policy,
                    missing_evidence="head_manifest",
                )
        if manifest is None:
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
        elif getattr(self.lifecycle, "enabled", False):
            base_evidence = self.lifecycle.get_manifest_evidence(
                organization_id=str(owner), repository_id=str(repository),
                commit_sha=event.base_sha)
            if base_evidence is not None:
                previous_manifest = base_evidence["manifest"]

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
        if (getattr(self.lifecycle, "enabled", False)
                and previous_manifest is None):
            return self._wait_for_manifest(
                event, client, config, base_manifest=None,
                head_manifest=manifest, expected_app_id=expected_app_id)
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
        # ---- authoritative PostgreSQL review lifecycle -------------------
        # This is the connection Release 1 was missing: the served webhook
        # path now records the review, and decides whether production
        # evidence is required, before anything is published to GitHub.
        outcome = self._begin_lifecycle(
            event,
            config,
            manifest=manifest,
            previous_manifest=previous_manifest,
            result=result,
        )

        publish_result = result
        status_label = "reviewed"
        if outcome is not None and outcome.waiting:
            # The review has not failed; it has not finished. Publish a
            # non-final waiting state rather than a verdict.
            publish_result = render_waiting_result(
                outcome, base_sha=event.base_sha, head_sha=event.head_sha)
            status_label = "waiting_for_metadata"

        published = self._publish(
            event,
            client,
            config.enforcement_mode,
            publish_result,
            status=status_label,
            expected_app_id=expected_app_id,
        )
        self._record_publication_identity(event, outcome, published)
        if outcome is not None:
            published["review_id"] = outcome.review_id
            published["review_attempt"] = outcome.attempt
            published["lifecycle_state"] = outcome.lifecycle_state
            published["collection_request_id"] = outcome.request_id
        return published

    def _wait_for_manifest(self, event, client, config, *, base_manifest,
                           head_manifest=None, expected_app_id):
        changed_files = client.compare_files(
            event.repository.owner, event.repository.name,
            event.base_sha, event.head_sha)
        if not getattr(changed_files, "complete", True):
            return self._publish_neutral(
                event, client, config.enforcement_mode,
                status="large_pr",
                message=(
                    "Relium could not safely enumerate every changed file in this "
                    "pull request. The review was skipped because GitHub may have "
                    "truncated the compare response."
                ),
                expected_app_id=expected_app_id,
            )
        outcome = self.lifecycle.wait_for_manifest(
            organization_id=str(event.repository.owner),
            repository_id=str(event.repository.name),
            pull_number=event.pull_number,
            base_sha=event.base_sha, head_sha=event.head_sha,
            base_manifest=base_manifest, head_manifest=head_manifest,
            changed_files=list(changed_files),
            enforcement_mode=config.enforcement_mode,
            delivery_id=event.delivery_id,
        )
        waiting_result = render_manifest_waiting_result(
            outcome, base_sha=event.base_sha, head_sha=event.head_sha)
        published = self._publish(
            event, client, config.enforcement_mode, waiting_result,
            status="waiting_for_manifest", expected_app_id=expected_app_id)
        self._record_publication_identity(event, outcome, published)
        published["review_id"] = outcome.review_id
        published["review_attempt"] = outcome.attempt
        published["lifecycle_state"] = outcome.lifecycle_state
        published["collection_request_id"] = None
        return published

    def _begin_lifecycle(self, event, config, *, manifest, previous_manifest,
                         result):
        """Persist the review through the lifecycle service.

        Returns None when the deterministic filesystem-compatibility mode is
        active. A lifecycle failure is never swallowed into a silent
        filesystem-only review: it propagates, so the delivery is retried
        rather than published as if it had been recorded.
        """
        if not getattr(self.lifecycle, "enabled", False):
            return None
        incident = result.get("incident") or {}
        health = incident.get("health")
        return self.lifecycle.begin(
            semantic_evidence=_semantic_evidence(incident),
            code_findings=lifecycle_code_findings(result),
            health_explanation=result.get("health_explanation"),
            organization_id=str(event.repository.owner),
            repository_id=str(event.repository.name),
            pull_number=event.pull_number,
            base_sha=event.base_sha,
            head_sha=event.head_sha,
            base_manifest=previous_manifest,
            head_manifest=manifest,
            changed_models=list(result.get("changed_models") or []),
            enforcement_mode=config.enforcement_mode,
            delivery_id=event.delivery_id,
            code_health=int(health) if isinstance(health, int) else 100,
        )

    def _record_publication_identity(self, event, outcome, published):
        """Persist the sticky comment and check-run identities.

        Recomputation reconciles these rather than publishing again.
        """
        if outcome is None or not getattr(self.lifecycle, "enabled", False):
            return
        comment = published.get("comment") or {}
        check = published.get("check") or {}
        self.lifecycle.record_publication(
            organization_id=str(event.repository.owner),
            repository_id=str(event.repository.name),
            review_id=outcome.review_id,
            comment_id=comment.get("id"),
            check_run_id=check.get("id"),
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
        evidence_policy=None,
        missing_evidence=None,
    ):
        result = {
            "decision": "SKIPPED",
            "rendered": {"markdown": f"## Relium deployment review\n\n{message}"},
        }
        if evidence_policy is not None and missing_evidence is not None:
            coverage = evaluate_evidence_policy(
                mode=enforcement_mode,
                policy=evidence_policy,
                evidence={missing_evidence: EvidenceState.MISSING},
                health=100,
            )
            result.update(coverage.to_dict())
            result["evidence_reasons"] = [message, *coverage.reasons]
            result["incident"] = {
                "decision": coverage.decision,
                "health": coverage.health,
                "severity": "LOW",
                "confidence": 0,
                "top_reasons": [message, *coverage.reasons],
                "recommendation": "Provide the required evidence before relying on this review.",
                "affected_models": [],
            }
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
        slack = self._publish_slack(event, publication_id, result)
        return {
            "status": status,
            "result": result,
            "comment": comment,
            "check": check,
            "slack": slack,
        }

    def _publish_slack(self, event, publication_id, result):
        if self.slack_publisher is None:
            return {
                "state": "disabled",
                "publication_id": publication_id,
            }
        try:
            journal = self.storage.get_publication_journal(
                event.repository.id,
                publication_id,
            )
            existing = journal.get("slack")
            if isinstance(existing, dict):
                return self._reconcile_slack_state(event, publication_id, existing)
            classification = self.slack_publisher.classify(result)
            if classification != "publish":
                outcome = {
                    "state": "skipped",
                    "publication_id": publication_id,
                    "reason": classification,
                }
                if self.storage.claim_publication_step(
                    event.repository.id,
                    publication_id,
                    "slack",
                    outcome,
                ):
                    return _safe_slack_result(outcome, publication_id)
                existing = self.storage.get_publication_journal(
                    event.repository.id,
                    publication_id,
                ).get("slack")
                return self._reconcile_slack_state(
                    event, publication_id, existing
                )
            intent = {"state": "started", "publication_id": publication_id}
            if not self.storage.claim_publication_step(
                event.repository.id,
                publication_id,
                "slack",
                intent,
            ):
                existing = self.storage.get_publication_journal(
                    event.repository.id,
                    publication_id,
                ).get("slack")
                return self._reconcile_slack_state(
                    event, publication_id, existing
                )
        except Exception:
            return {
                "state": "failed",
                "publication_id": publication_id,
                "error_category": "slack_state",
            }
        try:
            published = self.slack_publisher.publish(
                publication_id=publication_id,
                repository=event.repository.full_name,
                pull_number=event.pull_number,
                result=result,
                pull_url=(
                    f"https://github.com/{event.repository.owner}/"
                    f"{event.repository.name}/pull/{event.pull_number}"
                ),
            )
            outcome = _safe_slack_result(published, publication_id)
        except Exception:
            outcome = {
                "state": "failed",
                "publication_id": publication_id,
                "error_category": "slack_publication",
            }
        try:
            self.storage.record_publication_step(
                event.repository.id,
                publication_id,
                "slack",
                outcome,
            )
        except Exception:
            return {
                "state": "failed",
                "publication_id": publication_id,
                "error_category": "slack_state",
            }
        return outcome

    def _reconcile_slack_state(self, event, publication_id, existing):
        if isinstance(existing, dict) and existing.get("state") == "started":
            outcome = {
                "state": "indeterminate",
                "publication_id": publication_id,
                "reason": "prior_attempt_cannot_be_reconciled",
            }
            resolved = self.storage.transition_publication_step(
                event.repository.id,
                publication_id,
                "slack",
                expected_state="started",
                value=outcome,
            )
            return _safe_slack_result(resolved, publication_id)
        return _safe_slack_result(existing, publication_id)

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


def _safe_slack_result(value, publication_id):
    if not isinstance(value, dict):
        return {
            "state": "failed",
            "publication_id": publication_id,
            "error_category": "slack_result",
        }
    state = value.get("state")
    if state not in {"complete", "skipped", "failed", "indeterminate"}:
        state = "failed"
    result = {"state": state, "publication_id": publication_id}
    attempts = value.get("attempts")
    if isinstance(attempts, int) and not isinstance(attempts, bool) and attempts >= 0:
        result["attempts"] = attempts
    for key in ("reason", "error_category"):
        item = value.get(key)
        if isinstance(item, str) and item in {
            "decision_not_configured_for_slack",
            "decision_not_alertable",
            "prior_attempt_cannot_be_reconciled",
            "slack_http",
            "slack_rate_limit",
            "slack_server",
            "slack_network",
            "slack_transport",
            "slack_publication",
            "slack_result",
            "slack_state",
        }:
            result[key] = item
    if state == "failed" and "error_category" not in result:
        result["error_category"] = "slack_result"
    return result


def _semantic_evidence(incident) -> dict | None:
    """The SQL semantic comparison this review already produced.

    Lifted from the analysis result rather than recomputed: the SQL is parsed
    exactly once, during the review, and this is that same object on its way
    to storage.

    Returns None when no comparison ran at all — for example when the base
    manifest could not be fetched — so an absent comparison is stored as SQL
    NULL and can never be read back as "compared, nothing changed".
    """
    return semantic_evidence_from_incident(incident)
