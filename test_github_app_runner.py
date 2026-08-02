import copy
import hashlib
import hmac
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from agent.github_app.models import PullRequestEvent, Repository


def _event(delivery="delivery-1"):
    return PullRequestEvent(
        delivery_id=delivery,
        action="opened",
        installation_id=9,
        repository=Repository(id=12, owner="acme", name="analytics", full_name="acme/analytics"),
        pull_number=4,
        head_sha="head",
        base_sha="base",
        sender_login="octocat",
    )


class FakeClient:
    def __init__(self):
        self.comments = []
        self.checks = []
        self.config_content = (
            b"version: 1\nmanifest_path: build/manifest.json\n"
        )

    def get_file(self, owner, repository, path, ref):
        if path == "relium.yml":
            return self.config_content
        return json.dumps({"nodes": {}}).encode()

    def compare_files(self, owner, repository, base, head):
        return ["models/orders.sql"]

    def list_issue_comments(self, owner, repository, pull_number):
        return self.comments

    def create_issue_comment(self, owner, repository, pull_number, body):
        value = {
            "id": 1,
            "body": body,
            "performed_via_github_app": {"id": 123},
        }
        self.comments.append(value)
        return value

    def update_issue_comment(self, owner, repository, comment_id, body):
        value = {
            "id": comment_id,
            "body": body,
            "performed_via_github_app": {"id": 123},
        }
        for index, comment in enumerate(self.comments):
            if comment["id"] == comment_id:
                self.comments[index] = value
                break
        return value

    def create_check_run(self, owner, repository, payload):
        self.checks.append(payload)
        return {"id": 2}


class GitHubAppRunnerTests(unittest.TestCase):
    def test_runner_reuses_review_manifest_change_contract_and_publishes(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        reviewer = Mock(return_value={
            "decision": "ALLOW", "rendered": {"markdown": "## review"}
        })
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            runner = PullRequestReviewRunner(storage=RepositoryStorage(tmp), reviewer=reviewer)
            response = runner.run(_event(), client, expected_app_id=123)

        self.assertEqual(response["status"], "reviewed")
        reviewer.assert_called_once_with(
            manifest={"nodes": {}},
            previous_manifest={"nodes": {}},
            changed_files=["models/orders.sql"],
            deployment_id="github:12:head",
            manifest_source={"base": "github", "head": "github"},
            base_sha="base",
            head_sha="head",
        )
        self.assertEqual(client.checks[0]["conclusion"], "success")
        self.assertIn("relium-github-app-review", client.comments[0]["body"])

    def test_reviewed_block_comment_is_concise_actionable_and_sql_free(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        reviewer = Mock(return_value=_material_block_result())
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            runner = PullRequestReviewRunner(
                storage=RepositoryStorage(tmp),
                reviewer=reviewer,
            )
            runner.run(_event("actionable-comment"), client, expected_app_id=123)

        self.assertEqual(
            client.comments[0]["body"],
            "<!-- relium-github-app-review -->\n"
            "### Relium PR Guard — BLOCK\n\n"
            "This change may produce incorrect results in `revenue_refunds`.\n\n"
            "#### Why Relium blocked this PR\n\n"
            "**Division without a zero-safe guard**\n"
            "The denominator may be zero, causing an error or NULL result.\n"
            "**Fix:** Use `NULLIF(denominator, 0)` or an explicit `CASE` guard.\n\n"
            "**Integer division may truncate decimal values**\n"
            "Rates, averages, and percentages may lose their decimal portion.\n"
            "**Fix:** Cast one operand to `DECIMAL` or `FLOAT`.\n\n"
            "**Not-equal filter may silently exclude NULL rows**\n"
            "Rows with NULL values may be removed unintentionally.\n"
            "**Fix:** Handle NULL explicitly or use `IS DISTINCT FROM`.\n\n"
            "Decision: BLOCK\n"
            "Risk level: High\n"
            "Affected model: `revenue_refunds`",
        )
        self.assertNotIn("select customer_secret", client.comments[0]["body"])
        self.assertNotIn(
            "Review the flagged pipeline signals",
            client.comments[0]["body"],
        )

    def test_enforcement_mode_alone_controls_block_check_conclusion(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        scenarios = (
            (
                b"manifest_path: build/manifest.json\nmode: block\n",
                "neutral",
            ),
            (
                b"manifest_path: build/manifest.json\n"
                b"mode: block\nenforcement_mode: shadow\n",
                "neutral",
            ),
            (
                b"manifest_path: build/manifest.json\n"
                b"mode: warn\nenforcement_mode: enforce\n",
                "failure",
            ),
        )
        comments = []
        for index, (config_content, expected_conclusion) in enumerate(scenarios):
            with self.subTest(config=config_content):
                with tempfile.TemporaryDirectory() as tmp:
                    client = FakeClient()
                    client.config_content = config_content
                    response = PullRequestReviewRunner(
                        storage=RepositoryStorage(tmp),
                        reviewer=Mock(return_value=_material_block_result()),
                    ).run(
                        _event(f"enforcement-{index}"),
                        client,
                        expected_app_id=123,
                    )
                self.assertEqual(
                    client.checks[0]["conclusion"],
                    expected_conclusion,
                )
                comments.append(response["comment"]["body"])

        self.assertEqual(len(set(comments)), 1)

    def test_real_safe_review_is_allow_100_and_non_failing_in_shadow(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        client = FakeClient()
        client.config_content = (
            b"manifest_path: build/manifest.json\n"
            b"enforcement_mode: shadow\n"
        )
        client.get_file = _repository_file_loader(
            client,
            _review_manifest(
                "select customer_id, order_total from raw_orders "
                "where order_status = 'completed'"
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            response = PullRequestReviewRunner(
                storage=RepositoryStorage(tmp)
            ).run(
                _event("safe-shadow"),
                client,
                expected_app_id=123,
            )

        self.assertEqual(response["result"]["decision"], "ALLOW")
        self.assertEqual(response["result"]["incident"]["health"], 100)
        self.assertEqual(client.checks[0]["conclusion"], "success")
        self.assertIn("### Relium PR Guard — ALLOW", response["comment"]["body"])
        self.assertNotIn("Why Relium blocked", response["comment"]["body"])

    def test_l01_regression_preserves_semantic_graph_and_immutable_sha_binding(self):
        from agent.github_app.client import ChangedFiles
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        base_sha = "25ff8dd38f04f377ade840c4eaa65ba2538f5da8"
        head_sha = "a4d18c92736b5e8f04c120bd6ea1789f5502c36b"
        base_manifest = _l01_full_manifest()
        head_manifest = copy.deepcopy(base_manifest)
        safe_model = head_manifest["nodes"][
            "model.relium_e2e_dbt.stg_exchange_rates"
        ]
        safe_model["raw_code"] = (
            "-- L01 safe shadow-mode review\n"
            "select currency, rate from exchange_rates"
        )
        safe_model["compiled_code"] = safe_model["raw_code"]

        class L01Client(FakeClient):
            def __init__(self):
                super().__init__()
                self.file_requests = []
                self.config_content = (
                    b"enabled: true\n"
                    b"enforcement_mode: shadow\n"
                    b"manifest_path: artifacts/manifest.json\n"
                )

            def get_file(self, owner, repository, path, ref):
                self.file_requests.append((path, ref))
                if path == "relium.yml":
                    self.assert_ref(ref, head_sha)
                    return self.config_content
                self.assert_ref(path, "artifacts/manifest.json")
                if ref == base_sha:
                    return json.dumps(base_manifest).encode("utf-8")
                self.assert_ref(ref, head_sha)
                return json.dumps(head_manifest).encode("utf-8")

            def compare_files(self, owner, repository, base, head):
                self.assert_ref((base, head), (base_sha, head_sha))
                return ChangedFiles(
                    [
                        "artifacts/manifest.json",
                        "models/staging/stg_exchange_rates.sql",
                    ],
                    complete=True,
                )

            @staticmethod
            def assert_ref(actual, expected):
                if actual != expected:
                    raise AssertionError(f"expected {expected!r}, got {actual!r}")

        event = PullRequestEvent(
            delivery_id="fresh-corrected-l01-delivery",
            action="opened",
            installation_id=9,
            repository=Repository(
                id=1319166913,
                owner="AbhishekhPR",
                name="relium-e2e-dbt",
                full_name="AbhishekhPR/relium-e2e-dbt",
            ),
            pull_number=2,
            head_sha=head_sha,
            base_sha=base_sha,
            sender_login="AbhishekhPR",
        )

        from agent.deployment_history import DeploymentHistoryStore
        from agent.deployment_outcomes import DeploymentOutcomeStore
        from agent.deployment_review_service import review_manifest_change
        from agent.metadata_drift import compare_last_run
        from agent.metadata_store import (
            ModelMetricRecord,
            ScanRunRecord,
            insert_model_metrics,
            insert_scan_run,
        )

        with tempfile.TemporaryDirectory() as old_root:
            old_state = Path(old_root)
            prior_storage = RepositoryStorage(old_state / "runtime")
            self.assertTrue(
                prior_storage.claim_delivery(
                    event.repository.id, event.delivery_id
                )
            )
            prior_storage.complete_delivery(
                event.repository.id, event.delivery_id
            )
            DeploymentHistoryStore(old_state / "history.json").save_snapshot(
                {"snapshot_id": "poison-snapshot", "decision": "BLOCK"}
            )
            DeploymentOutcomeStore(old_state / "outcomes.json").save_outcome(
                {
                    "outcome_id": "poison-outcome",
                    "deployment_id": f"github:{event.repository.id}:{head_sha}",
                    "decision": "ALLOW",
                    "outcome": "incident_occurred",
                    "created_at": "2026-08-02T00:00:00Z",
                }
            )
            metadata_path = old_state / "metadata.db"
            for scan_id, timestamp, row_count, duplicate_count in (
                ("poison-previous", "2026-08-01T00:00:00+00:00", 100, 0),
                ("poison-current", "2026-08-02T00:00:00+00:00", 10, 10),
            ):
                insert_scan_run(
                    metadata_path,
                    ScanRunRecord(
                        scan_id=scan_id,
                        project_name="relium_e2e_dbt",
                        model_name="stg_exchange_rates",
                        risk_level="HIGH",
                        safe_to_merge=False,
                        report_text="poison drift",
                        timestamp=timestamp,
                    ),
                )
                insert_model_metrics(
                    metadata_path,
                    ModelMetricRecord(
                        scan_id=scan_id,
                        project_name="relium_e2e_dbt",
                        model_name="stg_exchange_rates",
                        row_count=row_count,
                        null_count=0,
                        duplicate_count=duplicate_count,
                        freshness_timestamp=timestamp,
                        schema_column_count=3,
                        timestamp=timestamp,
                    ),
                )
            self.assertEqual(
                compare_last_run(
                    metadata_path,
                    "relium_e2e_dbt",
                    "stg_exchange_rates",
                )["drift_level"],
                "HIGH",
            )
            poisoned = review_manifest_change(
                manifest=head_manifest,
                previous_manifest=base_manifest,
                changed_files=[
                    "artifacts/manifest.json",
                    "models/staging/stg_exchange_rates.sql",
                ],
                deployment_id=f"github:{event.repository.id}:{head_sha}",
                outcomes_path=old_state / "outcomes.json",
                metadata_db_path=metadata_path,
                manifest_source={"base": "github", "head": "github"},
                base_sha=base_sha,
                head_sha=head_sha,
            )
            self.assertNotEqual(poisoned["decision"], "ALLOW")
            with tempfile.TemporaryDirectory() as isolated_root:
                self.assertEqual(list(Path(isolated_root).iterdir()), [])
                client = L01Client()
                runner = PullRequestReviewRunner(
                    storage=RepositoryStorage(isolated_root)
                )
                response = runner.run(event, client, expected_app_id=123)
                replay = runner.run(
                    replace(event, delivery_id="fresh-corrected-l01-replay"),
                    client,
                    expected_app_id=123,
                )

        self.assertEqual(response["result"]["decision"], "ALLOW")
        self.assertEqual(response["result"]["incident"]["health"], 100)
        self.assertEqual(response["result"]["changed_models"], ["stg_exchange_rates"])
        self.assertEqual(client.checks[0]["conclusion"], "success")
        self.assertEqual(len(client.comments), 1)
        self.assertEqual(len(client.checks), 1)
        self.assertEqual(replay["status"], "reviewed")
        metadata = response["result"]["incident"]["metadata"]
        self.assertFalse(metadata["history_enabled"])
        self.assertTrue(
            all(
                item["comparison_status"] == "unavailable"
                for item in metadata["metadata_drift"]
            )
        )
        self.assertNotIn(
            "deployment_outcomes",
            response["result"]["incident"]["signal_components"],
        )
        self.assertEqual(
            client.file_requests,
            [
                ("relium.yml", head_sha),
                ("artifacts/manifest.json", head_sha),
                ("artifacts/manifest.json", base_sha),
                ("relium.yml", head_sha),
                ("artifacts/manifest.json", head_sha),
                ("artifacts/manifest.json", base_sha),
            ],
        )

    def test_l01_captured_synthetic_manifest_replacement_remains_blocked(self):
        from agent.deployment_review_service import review_manifest_change

        result = review_manifest_change(
            manifest=_l01_synthetic_manifest(),
            previous_manifest=_l01_full_manifest(),
            changed_files=[
                "artifacts/manifest.json",
                "models/scenario_model.sql",
            ],
            deployment_id="github:1319166913:9bcc25fe96c18d14daa0188b71b625e5f8de2892",
            manifest_source={"base": "github", "head": "github"},
            base_sha="25ff8dd38f04f377ade840c4eaa65ba2538f5da8",
            head_sha="9bcc25fe96c18d14daa0188b71b625e5f8de2892",
        )

        self.assertEqual(result["decision"], "BLOCK")
        self.assertEqual(result["incident"]["health"], 65)
        self.assertEqual(
            result["incident"]["top_reasons"][:3],
            [
                "Churn / Retention KPI was removed",
                "Recurring Revenue KPI was removed",
                "Revenue / GMV KPI was removed",
            ],
        )
        self.assertEqual(result["changed_models"], ["scenario_model"])

    def test_real_risky_review_is_block_65_with_sticky_identical_comments(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        risky_sql = (
            "select customer_id, sum(order_total) / count(*) "
            "as average_order_value from raw_orders "
            "where order_status != 'cancelled' group by customer_id"
        )
        client = FakeClient()
        client.get_file = _repository_file_loader(
            client,
            _review_manifest(risky_sql),
        )
        with tempfile.TemporaryDirectory() as tmp:
            runner = PullRequestReviewRunner(storage=RepositoryStorage(tmp))
            client.config_content = (
                b"manifest_path: build/manifest.json\n"
                b"enforcement_mode: shadow\n"
            )
            shadow = runner.run(
                _event("risky-shadow"),
                client,
                expected_app_id=123,
            )
            client.config_content = (
                b"manifest_path: build/manifest.json\n"
                b"enforcement_mode: enforce\n"
            )
            enforce = runner.run(
                _event("risky-enforce"),
                client,
                expected_app_id=123,
            )

        self.assertEqual(shadow["result"]["decision"], "BLOCK")
        self.assertEqual(shadow["result"]["incident"]["health"], 65)
        self.assertEqual(enforce["result"]["decision"], "BLOCK")
        self.assertEqual(enforce["result"]["incident"]["health"], 65)
        self.assertEqual(client.checks[0]["conclusion"], "neutral")
        self.assertEqual(client.checks[1]["conclusion"], "failure")
        self.assertEqual(shadow["comment"]["body"], enforce["comment"]["body"])
        self.assertEqual(len(client.comments), 1)
        self.assertIn(
            "**Division without a zero-safe guard**",
            enforce["comment"]["body"],
        )
        self.assertIn(
            "**Fix:** Use `NULLIF(denominator, 0)` or an explicit `CASE` guard.",
            enforce["comment"]["body"],
        )
        self.assertNotIn(risky_sql, enforce["comment"]["body"])

    def test_non_ast_warn_uses_only_material_reasons_and_recommendation(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        result = {
            "decision": "WARN",
            "incident": {
                "health": 85,
                "severity": "MEDIUM",
                "affected_models": ["customer_orders"],
                "top_reasons": ["A required contract assumption changed."],
                "recommendation": "Verify the changed contract with its owner.",
            },
            "material_findings": [],
            "rendered": {
                "markdown": (
                    "Metadata checks were not evaluated.\n"
                    "A required contract assumption changed."
                )
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            response = PullRequestReviewRunner(
                storage=RepositoryStorage(tmp),
                reviewer=Mock(return_value=result),
            ).run(
                _event("contract-warn"),
                client,
                expected_app_id=123,
            )

        comment = response["comment"]["body"]
        self.assertNotIn("No material deployment risks detected.", comment)
        self.assertEqual(
            comment.count("A required contract assumption changed."),
            1,
        )
        self.assertIn(
            "**Recommendation:** Verify the changed contract with its owner.",
            comment,
        )
        self.assertNotIn("Metadata checks were not evaluated.", comment)

    def test_duplicate_delivery_does_not_review_or_publish(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        reviewer = Mock(return_value={"decision": "ALLOW", "rendered": {"markdown": "ok"}})
        with tempfile.TemporaryDirectory() as tmp:
            runner = PullRequestReviewRunner(storage=RepositoryStorage(tmp), reviewer=reviewer)
            client = FakeClient()
            runner.run(_event(), client, expected_app_id=123)
            response = runner.run(_event(), client, expected_app_id=123)
        self.assertEqual(response["status"], "duplicate")
        reviewer.assert_called_once()

    def test_disabled_repository_does_not_load_manifest(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        client = Mock()
        client.get_file.return_value = b"version: 1\nenabled: false\n"
        with tempfile.TemporaryDirectory() as tmp:
            response = PullRequestReviewRunner(storage=RepositoryStorage(tmp)).run(
                _event(), client, expected_app_id=123
            )
        self.assertEqual(response["status"], "disabled")
        client.get_file.assert_called_once()

    def test_publication_failure_releases_delivery_for_retry(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        reviewer = Mock(return_value={"decision": "ALLOW", "rendered": {"markdown": "ok"}})
        with tempfile.TemporaryDirectory() as tmp:
            runner = PullRequestReviewRunner(storage=RepositoryStorage(tmp), reviewer=reviewer)
            client = FakeClient()
            client.create_check_run = Mock(side_effect=RuntimeError("publication failed"))
            with self.assertRaisesRegex(RuntimeError, "publication failed"):
                runner.run(_event(), client, expected_app_id=123)
            client.create_check_run = Mock(return_value={"id": 2})
            self.assertEqual(
                runner.run(_event(), client, expected_app_id=123)["status"],
                "reviewed",
            )

    def test_missing_manifest_publishes_actionable_neutral_result(self):
        from agent.github_app.client import GitHubNotFoundError
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        client = FakeClient()
        original = client.get_file

        def get_file(owner, repository, path, ref):
            if path == "build/manifest.json":
                raise GitHubNotFoundError("missing", status_code=404)
            return original(owner, repository, path, ref)

        client.get_file = get_file
        reviewer = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            response = PullRequestReviewRunner(
                storage=RepositoryStorage(tmp), reviewer=reviewer
            ).run(
                _event(), client, expected_app_id=123
            )
        self.assertEqual(response["status"], "missing_manifest")
        self.assertEqual(client.checks[0]["conclusion"], "neutral")
        self.assertIn(
            "Relium could not find build/manifest.json. "
            "Generate the configured dbt manifest before the Relium review.",
            client.comments[0]["body"],
        )
        reviewer.assert_not_called()

    def test_missing_config_uses_defaults_and_missing_manifest_is_actionable(self):
        from agent.github_app.client import GitHubNotFoundError
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        client = FakeClient()

        def get_file(owner, repository, path, ref):
            raise GitHubNotFoundError("missing", status_code=404)

        client.get_file = get_file
        reviewer = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            response = PullRequestReviewRunner(
                storage=RepositoryStorage(tmp), reviewer=reviewer
            ).run(
                _event("missing-defaults"), client, expected_app_id=123
            )
        self.assertEqual(response["status"], "missing_manifest")
        self.assertEqual(client.checks[0]["conclusion"], "neutral")
        self.assertIn(
            "Relium could not find target/manifest.json. "
            "Run dbt compile before the Relium review.",
            client.comments[0]["body"],
        )
        reviewer.assert_not_called()

    def test_non_404_config_error_is_not_treated_as_missing(self):
        from agent.github_app.client import GitHubAPIError
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        for status in (401, 403, 500):
            with self.subTest(status=status):
                client = FakeClient()
                client.get_file = Mock(
                    side_effect=GitHubAPIError("request failed", status_code=status)
                )
                with tempfile.TemporaryDirectory() as tmp:
                    with self.assertRaises(GitHubAPIError) as raised:
                        PullRequestReviewRunner(storage=RepositoryStorage(tmp)).run(
                            _event(f"api-error-{status}"), client, expected_app_id=123
                        )
                self.assertEqual(raised.exception.status_code, status)
                self.assertEqual(client.comments, [])
                self.assertEqual(client.checks, [])

    def test_no_changed_models_publishes_neutral_skipped_result(self):
        from agent.github_app.runner import PullRequestReviewRunner
        from agent.github_app.storage import RepositoryStorage

        reviewer = Mock(side_effect=ValueError("At least one changed model is required."))
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            response = PullRequestReviewRunner(
                storage=RepositoryStorage(tmp), reviewer=reviewer
            ).run(_event(), client, expected_app_id=123)
        self.assertEqual(response["status"], "skipped")
        self.assertEqual(client.checks[0]["conclusion"], "neutral")

    def test_adapter_validates_signature_authenticates_and_injects_client(self):
        from agent.github_app.adapter import GitHubAppAdapter

        body = json.dumps({
            "action": "opened", "installation": {"id": 9},
            "repository": {"id": 12, "name": "analytics", "full_name": "acme/analytics", "owner": {"login": "acme"}},
            "pull_request": {"number": 4, "head": {"sha": "head"}, "base": {"sha": "base"}},
            "sender": {"login": "octocat"},
        }).encode()
        signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        installation_client = Mock()
        installation_client.create_installation_access_token.return_value = {
            "token": "token",
            "permissions": {
                "checks": "write",
                "contents": "read",
                "issues": "write",
                "metadata": "read",
                "pull_requests": "write",
            },
        }
        scoped_client = Mock()
        installation_client.with_token.return_value = scoped_client
        runner = Mock()
        runner.run.return_value = {"status": "reviewed"}
        adapter = GitHubAppAdapter(
            webhook_secret="secret", app_id=1, private_key="key", runner=runner,
            client_factory=Mock(return_value=installation_client),
            jwt_factory=Mock(return_value="jwt"),
        )
        response = adapter.handle(event_name="pull_request", delivery_id="d-1", signature=signature, body=body)
        self.assertEqual(response["status"], "reviewed")
        installation_client.create_installation_access_token.assert_called_once_with(9, "jwt")
        installation_client.with_token.assert_called_once_with("token")
        runner.run.assert_called_once_with(
            runner.run.call_args.args[0], scoped_client, expected_app_id=1
        )

    def test_adapter_rejects_bad_signature_before_authentication(self):
        from agent.github_app.adapter import GitHubAppAdapter

        factory = Mock()
        adapter = GitHubAppAdapter(
            webhook_secret="secret", app_id=1, private_key="key", runner=Mock(), client_factory=factory
        )
        with self.assertRaises(PermissionError):
            adapter.handle(event_name="pull_request", delivery_id="d-1", signature="bad", body=b"{}")
        factory.assert_not_called()


def _material_block_result():
    return {
        "decision": "BLOCK",
        "incident": {
            "health": 65,
            "severity": "HIGH",
            "affected_models": ["revenue_refunds"],
        },
        "material_findings": [
            {
                "rule": "DIVISION_BY_ZERO",
                "title": "Division without a zero-safe guard",
                "impact": (
                    "Dividing by a customer expression can fail. "
                    "select customer_secret from raw_orders"
                ),
                "affected_model": "revenue_refunds",
                "recommended_fix": "Use a safe denominator.",
            },
            {
                "rule": "INTEGER_DIVISION",
                "title": "Integer division may truncate decimal values",
                "impact": "Integer division can truncate results.",
                "affected_model": "revenue_refunds",
                "recommended_fix": "Cast an operand.",
            },
            {
                "rule": "NOT_EQUAL_NULL_RISK",
                "title": "Not-equal filter may silently exclude NULL rows",
                "impact": "NULL rows may be excluded.",
                "affected_model": "revenue_refunds",
                "recommended_fix": "Handle NULL explicitly.",
            },
        ],
        "rendered": {
            "markdown": (
                "select customer_secret from raw_orders\n"
                "Review the flagged pipeline signals before deployment."
            )
        },
    }


def _review_manifest(sql):
    return {
        "nodes": {
            "model.analytics.revenue_refunds": {
                "resource_type": "model",
                "name": "revenue_refunds",
                "unique_id": "model.analytics.revenue_refunds",
                "original_file_path": "models/orders.sql",
                "raw_code": sql,
                "compiled_code": sql,
                "columns": {
                    "customer_id": {"name": "customer_id"},
                    "revenue": {"name": "revenue"},
                },
            }
        }
    }


def _l01_model(name, path, sql, columns, *, depends_on=()):
    return {
        "resource_type": "model",
        "name": name,
        "unique_id": f"model.relium_e2e_dbt.{name}",
        "original_file_path": path,
        "raw_code": sql,
        "compiled_code": sql,
        "columns": {column: {"name": column} for column in columns},
        "depends_on": {
            "nodes": [
                f"model.relium_e2e_dbt.{dependency}"
                for dependency in depends_on
            ]
        },
    }


def _l01_full_manifest():
    models = (
        _l01_model(
            "fct_revenue_daily",
            "models/marts/finance/fct_revenue_daily.sql",
            "select revenue_date, net_revenue from orders",
            ("revenue_date", "net_revenue"),
        ),
        _l01_model(
            "exec_revenue_dashboard",
            "models/executive/exec_revenue_dashboard.sql",
            "select revenue_date, net_revenue from fct_revenue_daily",
            ("revenue_date", "net_revenue"),
            depends_on=("fct_revenue_daily",),
        ),
        _l01_model(
            "fct_subscription_revenue",
            "models/marts/finance/fct_subscription_revenue.sql",
            "select subscription_id, mrr from subscriptions",
            ("subscription_id", "mrr"),
        ),
        _l01_model(
            "subscription_churn",
            "models/marts/subscriptions/subscription_churn.sql",
            "select customer_id, churned from subscriptions",
            ("customer_id", "churned"),
        ),
        _l01_model(
            "stg_exchange_rates",
            "models/staging/stg_exchange_rates.sql",
            "select currency, rate from exchange_rates",
            ("currency", "rate"),
        ),
    )
    return {
        "metadata": {"project_name": "relium_e2e_dbt"},
        "nodes": {model["unique_id"]: model for model in models},
    }


def _l01_synthetic_manifest():
    model = _l01_model(
        "scenario_model",
        "models/scenario_model.sql",
        (
            "select cast(numerator as real) / nullif(denominator, 0) as ratio "
            "from source_data"
        ),
        ("ratio",),
    )
    model["unique_id"] = "model.relium_e2e_live.scenario_model"
    return {
        "metadata": {"project_name": "relium_e2e_live"},
        "nodes": {model["unique_id"]: model},
    }


def _repository_file_loader(client, manifest):
    def get_file(owner, repository, path, ref):
        if path == "relium.yml":
            return client.config_content
        return json.dumps(manifest).encode()

    return get_file


if __name__ == "__main__":
    unittest.main()
