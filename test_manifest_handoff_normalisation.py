"""The proposed fix for the PR #46 409, proved end to end.

Extracts the normalisation the workflow now applies and runs it against the
REAL route, so the claim "this makes re-submission idempotent" is demonstrated
rather than asserted.

Also proves the safety invariant survives: genuinely different manifest content
for the same commit is still rejected.

NO REAL CREDENTIAL APPEARS IN THIS FILE.
"""
from __future__ import annotations

import json
import os
import re
import unittest
from pathlib import Path

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

ORG = "AbhishekhPR"
REPO = "dbt_agent"
WORKFLOW = Path(".github/workflows/relium-pr-review.yml")

_SEQUENCE = iter(range(20_000, 30_000))


def _sha():
    return f"{next(_SEQUENCE):040x}".replace("x", "c")


def _workflow_normaliser():
    """Load `stable()` from the workflow itself.

    Copying the function into the test would let the two drift apart silently
    and prove nothing about what CI actually sends. This executes the real
    source, so if someone edits the workflow the test follows.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    # The workflow has more than one embedded python step (compile, then
    # submit). Take the one that actually defines the helper rather than
    # assuming a position, which is how this silently read the wrong block.
    blocks = [chunk.split("\n          PY", 1)[0]
              for chunk in text.split("python - <<'PY'")[1:]]
    matching = [chunk for chunk in blocks if "VOLATILE_METADATA" in chunk]
    assert matching, "no embedded python step defines VOLATILE_METADATA"
    code = "\n".join(
        line[10:] if line.startswith(" " * 10) else line
        for line in matching[0].splitlines())
    # Just the helper and its constant; the surrounding script needs
    # environment and files that do not exist here. Extracted by line span
    # rather than a regex, which is brittle against the comment block above
    # the constant.
    lines = code.splitlines()
    start = next(i for i, line in enumerate(lines)
                 if line.startswith("VOLATILE_METADATA"))
    end = next(i for i, line in enumerate(lines)
               if i > start and line.strip() == "return manifest")
    namespace = {}
    exec(compile("\n".join(lines[start:end + 1]), "workflow-stable", "exec"),
         namespace)
    assert "stable" in namespace, "the workflow no longer defines stable()"
    return namespace["stable"]


def _manifest(*, generated_at, invocation_id, revenue_sql="select 1 as revenue"):
    return {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "dbt_version": "1.8.0",
            "generated_at": generated_at,
            "invocation_id": invocation_id,
            "project_name": "relium",
        },
        "nodes": {
            "model.relium.fct_revenue": {
                "resource_type": "model", "name": "fct_revenue",
                "database": "analytics", "schema": "public",
                "raw_code": revenue_sql,
            },
        },
        "sources": {}, "child_map": {}, "parent_map": {},
    }


class _StubQueue:
    is_running = False

    def start(self):
        self.is_running = True

    def stop(self, timeout=None):
        self.is_running = False

    def enqueue(self, job):
        return True


class NormalisationUnitTests(unittest.TestCase):
    """The helper itself, without a database."""

    def setUp(self):
        self.stable = _workflow_normaliser()

    def test_it_removes_only_the_volatile_run_metadata(self):
        manifest = _manifest(generated_at="2026-08-18T10:00:00Z",
                             invocation_id="run-1")
        result = self.stable(manifest)
        self.assertNotIn("generated_at", result["metadata"])
        self.assertNotIn("invocation_id", result["metadata"])
        # Everything the review path reads survives untouched.
        self.assertEqual(result["nodes"], manifest["nodes"])
        self.assertEqual(result["metadata"]["dbt_version"], "1.8.0")
        self.assertEqual(result["metadata"]["project_name"], "relium")

    def test_two_compiles_of_the_same_commit_normalise_to_one_document(self):
        first = self.stable(_manifest(generated_at="2026-08-18T10:00:00Z",
                                      invocation_id="run-1"))
        second = self.stable(_manifest(generated_at="2026-08-19T09:15:00Z",
                                       invocation_id="run-2"))
        self.assertEqual(first, second)

    def test_genuinely_different_content_still_differs(self):
        first = self.stable(_manifest(generated_at="t1", invocation_id="i1"))
        second = self.stable(_manifest(generated_at="t1", invocation_id="i1",
                                       revenue_sql="select 2 as revenue"))
        self.assertNotEqual(first, second)

    def test_it_does_not_mutate_the_caller_manifest(self):
        manifest = _manifest(generated_at="t1", invocation_id="i1")
        self.stable(manifest)
        self.assertIn("generated_at", manifest["metadata"])

    def test_a_manifest_without_metadata_is_passed_through(self):
        self.assertEqual(self.stable({"nodes": {}}), {"nodes": {}})


class WorkflowSafetyTests(unittest.TestCase):
    """Static checks on the workflow. No database, no network.

    These run everywhere, including where PostgreSQL is unavailable, because
    "does the CI step leak a credential" should never be a question that only
    gets answered on a machine with a database.
    """

    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")
        blocks = [chunk.split("\n          PY", 1)[0]
                  for chunk in self.text.split("python - <<\'PY\'")[1:]]
        matching = [chunk for chunk in blocks if "VOLATILE_METADATA" in chunk]
        self.assertTrue(matching, "no embedded step defines VOLATILE_METADATA")
        self.code = "\n".join(
            line[10:] if line.startswith(" " * 10) else line
            for line in matching[0].splitlines())

    def test_the_workflow_yaml_parses(self):
        import yaml

        document = yaml.safe_load(self.text)
        self.assertIsInstance(document, dict)
        self.assertIn("jobs", document)

    def test_the_embedded_python_compiles(self):
        compile(self.code, "relium-pr-review", "exec")

    def test_exactly_three_volatile_fields_are_removed(self):
        """Scope discipline. Removing more than was proven volatile would be
        stripping evidence, not noise."""
        namespace = {}
        lines = self.code.splitlines()
        start = next(i for i, line in enumerate(lines)
                     if line.startswith("VOLATILE_METADATA"))
        end = next(i for i, line in enumerate(lines)
                   if i > start and line.strip() == "return manifest")
        exec(compile("\n".join(lines[start:end + 1]), "w", "exec"), namespace)
        self.assertEqual(
            set(namespace["VOLATILE_METADATA"]),
            {"generated_at", "invocation_id", "invocation_started_at"})

    def test_the_diagnostics_print_only_safe_response_fields(self):
        """`describe` reads three keys and nothing else."""
        self.assertIn('for key in ("status", "code", "detail")', self.code)

    def test_nothing_prints_a_credential_or_the_payload(self):
        """What reaches the log, checked on the interpolated values only.

        A substring scan over whole lines is too crude: `len(body)` is the
        payload SIZE, which is useful and safe, while `body` would be the
        payload itself. So this inspects only what is interpolated into an
        f-string that gets printed.
        """
        printed = [line.strip() for line in self.code.splitlines()
                   if "print(" in line or "SystemExit(" in line]
        interpolated = set()
        for line in printed:
            interpolated.update(re.findall(r"\{([^}]+)\}", line))

        for expression in interpolated:
            for forbidden in ("token", "Authorization", "manifest",
                              "request.data", "payload"):
                self.assertNotIn(
                    forbidden, expression,
                    f"{expression!r} reaches a public CI log")
        # The size is deliberately kept: it is what diagnosed the 413 theory.
        self.assertIn("len(body)", interpolated)

    def test_the_manifest_is_never_interpolated_anywhere(self):
        """Belt and braces, across the whole step rather than print sites."""
        self.assertNotIn("{manifest}", self.code)
        self.assertNotIn("{body}", self.code)
        self.assertNotIn("{token}", self.code.replace(
            'f"Bearer {token}"', ""))

    def test_the_token_is_only_ever_read_or_sent_as_a_header(self):
        """Three legitimate uses: read it, check it is present, send it.

        Anything else — logging it, putting it in a URL, writing it to a file
        — would be a leak.

        Parsed with `ast`, not scanned as text: a comment or docstring that
        mentions the token is not a use of it, and a scanner that cannot tell
        the difference produces noise nobody acts on.
        """
        import ast

        tree = ast.parse(self.code)
        uses = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "token":
                uses.append(node)

        # One binding (the assignment), one presence check, one header value.
        self.assertEqual(len(uses), 3, f"{len(uses)} references to `token`")

        # The only place its VALUE is interpolated is the Authorization header.
        interpolations = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.JoinedStr)
            and any(isinstance(part, ast.FormattedValue)
                    and isinstance(part.value, ast.Name)
                    and part.value.id == "token"
                    for part in node.values)
        ]
        self.assertEqual(len(interpolations), 1)
        rendered = ast.unparse(interpolations[0])
        self.assertIn("Bearer", rendered)

    def test_an_http_error_is_caught_rather_than_escaping(self):
        """The original defect: urllib raised and the response body was lost."""
        self.assertIn("except HTTPError as error:", self.code)
        self.assertIn("except URLError as error:", self.code)

    def test_no_backend_file_is_touched_by_this_fix(self):
        """The invariant lives in the backend and is not being edited."""
        import subprocess

        changed = subprocess.run(
            ["git", "diff", "--name-only"], capture_output=True,
            text=True).stdout.split()
        for path in changed:
            self.assertFalse(
                path.startswith("agent/"),
                f"{path} is a backend file; this fix is workflow-only")


@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set; the conflict is a database property")
class NormalisedSubmissionTests(unittest.TestCase):
    """The fix against the real route and a real PostgreSQL."""

    @classmethod
    def setUpClass(cls):
        import psycopg
        from starlette.testclient import TestClient

        from agent.api.pool import StorePool
        from agent.github_app.http_app import create_http_app
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")

        cls.pool = StorePool(lambda: PostgresLifecycleStore(DSN), size=3)
        cls.app = create_http_app(
            webhook_secret="norm-secret", job_queue=_StubQueue(),
            max_body_bytes=8 * 1024 * 1024, shutdown_timeout_seconds=1.0,
            clock=lambda: 0.0, store_pool=cls.pool)
        cls.http = TestClient(cls.app)
        cls.http.__enter__()

        from agent.collector.provisioning import issue_ci_token

        with cls.pool.acquire() as store:
            store.ensure_repository(ORG, REPO)
            _, cls.token = issue_ci_token(store, organization_id=ORG,
                                          repository_id=REPO)

    @classmethod
    def tearDownClass(cls):
        cls.http.__exit__(None, None, None)
        cls.pool.close()

    def setUp(self):
        self.stable = _workflow_normaliser()
        self.sha = _sha()

    def _submit(self, sha, manifest):
        return self.http.post(
            "/api/manifest-evidence",
            headers={"Authorization": f"Bearer {self.token}",
                     "Idempotency-Key": f"github-actions:123456789:{sha}"},
            json={"commit_sha": sha, "manifest": self.stable(manifest)})

    def test_recompiling_the_same_commit_is_now_idempotent(self):
        """THE FIX. This is the request that returned 409 for PR #46."""
        first = self._submit(self.sha, _manifest(
            generated_at="2026-08-18T10:00:00Z", invocation_id="run-1"))
        second = self._submit(self.sha, _manifest(
            generated_at="2026-08-19T09:15:00Z", invocation_id="run-2"))

        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertIs(second.json()["created"], False)
        self.assertEqual(first.json()["evidence_id"],
                         second.json()["evidence_id"])

    def test_the_safety_invariant_is_preserved(self):
        """Genuinely different evidence for one commit is STILL rejected.

        This is the check that must not be lost. A commit whose manifest
        content actually changed is a real conflict, and the API must keep
        saying so.
        """
        self._submit(self.sha, _manifest(
            generated_at="2026-08-18T10:00:00Z", invocation_id="run-1"))
        response = self._submit(self.sha, _manifest(
            generated_at="2026-08-19T09:15:00Z", invocation_id="run-2",
            revenue_sql="select 2 as revenue"))

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            response.json()["detail"],
            "idempotency key already used with different manifest evidence")

    def test_the_whole_base_and_head_handoff_succeeds_on_a_rerun(self):
        """The end state PR #46 needs: both sides submitted, twice."""
        base, head = _sha(), _sha()
        for attempt, stamp in enumerate(("run-1", "run-2"), start=1):
            for side, sha in (("base", base), ("head", head)):
                response = self._submit(sha, _manifest(
                    generated_at=f"2026-08-1{attempt}T10:00:00Z",
                    invocation_id=stamp))
                self.assertIn(response.status_code, (200, 202),
                              f"{side} attempt {attempt}: {response.text}")


if __name__ == "__main__":
    unittest.main()
