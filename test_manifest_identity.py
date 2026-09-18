"""Server-side semantic identity for manifest evidence.

No database. These pin the canonicalisation rules themselves, and pin them
against the copy embedded in the CI workflow -- the two definitions describe
the same thing, and a drift between them is how the original 409 became
dependent on which client version wrote first.

Two of the fixtures are REAL dbt manifests: two compiles of the same base
commit forty-one minutes apart. A synthetic manifest cannot stand in for them,
because the first attempt at this fix passed against a synthetic fixture and
still failed in CI -- the fixture carried none of the per-node `created_at`
stamps a real manifest has on every entry.
"""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from agent.metadata_evidence.manifest_identity import (
    CANONICALIZATION_VERSION,
    ENTRY_SECTIONS,
    LEGACY_CANONICALIZATION_VERSION,
    VOLATILE_ENTRY_FIELDS,
    VOLATILE_METADATA,
    canonical_manifest,
    semantic_manifest_hash,
    stored_semantic_hash,
)

FIXTURES = Path("tests/fixtures/manifests")
WORKFLOW = Path("agent/ci_workflow/relium-pr-review.yml")


def _real(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _manifest(*, generated_at="2026-09-01T10:00:00Z", invocation_id="run-1",
              created_at=1756720000.0, sql="select 1 as revenue"):
    return {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "dbt_version": "1.8.0",
            "project_name": "relium",
            "generated_at": generated_at,
            "invocation_id": invocation_id,
            "user_id": "8b1d-anonymous",
        },
        "nodes": {
            "model.relium.fct_revenue": {
                "unique_id": "model.relium.fct_revenue",
                "resource_type": "model",
                "name": "fct_revenue",
                "database": "analytics",
                "schema": "public",
                "alias": "fct_revenue",
                "raw_code": sql,
                "compiled_code": sql,
                "depends_on": {"nodes": []},
                "columns": {"revenue": {"name": "revenue"}},
                "created_at": created_at,
            },
        },
        "macros": {
            "macro.relium.cents_to_dollars": {
                "unique_id": "macro.relium.cents_to_dollars",
                "name": "cents_to_dollars",
                "macro_sql": "cents / 100",
                "created_at": created_at,
            },
        },
        "sources": {},
        "child_map": {},
        "parent_map": {},
    }


class CanonicalisationTests(unittest.TestCase):
    def test_a_manifest_is_its_own_identity(self):
        manifest = _manifest()
        self.assertEqual(semantic_manifest_hash(manifest),
                         semantic_manifest_hash(copy.deepcopy(manifest)))

    def test_run_metadata_does_not_change_the_identity(self):
        """Every field dbt stamps per compile, one at a time."""
        baseline = semantic_manifest_hash(_manifest())
        for field in VOLATILE_METADATA:
            document = _manifest()
            document["metadata"][field] = "changed-by-this-run"
            self.assertEqual(
                semantic_manifest_hash(document), baseline,
                f"metadata.{field} must not affect manifest identity")

    def test_per_entry_parse_timestamps_do_not_change_the_identity(self):
        self.assertEqual(
            semantic_manifest_hash(_manifest(created_at=1756720000.0)),
            semantic_manifest_hash(_manifest(created_at=1799999999.0)))

    def test_a_changed_model_still_changes_the_identity(self):
        self.assertNotEqual(
            semantic_manifest_hash(_manifest(sql="select 1 as revenue")),
            semantic_manifest_hash(_manifest(sql="select 2 as revenue")))

    def test_a_removed_model_still_changes_the_identity(self):
        stripped = _manifest()
        stripped["nodes"] = {}
        self.assertNotEqual(semantic_manifest_hash(stripped),
                            semantic_manifest_hash(_manifest()))

    def test_a_changed_column_set_still_changes_the_identity(self):
        widened = _manifest()
        widened["nodes"]["model.relium.fct_revenue"]["columns"]["currency"] = {
            "name": "currency"}
        self.assertNotEqual(semantic_manifest_hash(widened),
                            semantic_manifest_hash(_manifest()))

    def test_a_changed_dependency_still_changes_the_identity(self):
        rewired = _manifest()
        rewired["nodes"]["model.relium.fct_revenue"]["depends_on"] = {
            "nodes": ["model.relium.stg_payments"]}
        self.assertNotEqual(semantic_manifest_hash(rewired),
                            semantic_manifest_hash(_manifest()))

    def test_every_semantic_field_survives(self):
        node = canonical_manifest(_manifest())["nodes"]["model.relium.fct_revenue"]
        for field in ("unique_id", "resource_type", "name", "database",
                      "schema", "alias", "raw_code", "compiled_code",
                      "depends_on", "columns"):
            self.assertIn(field, node)

    def test_the_caller_manifest_is_not_mutated(self):
        manifest = _manifest()
        before = copy.deepcopy(manifest)
        canonical_manifest(manifest)
        semantic_manifest_hash(manifest)
        self.assertEqual(manifest, before)

    def test_only_nodes_and_macros_are_swept(self):
        """Widening the rule must be argued for here first."""
        self.assertEqual(set(ENTRY_SECTIONS), {"nodes", "macros"})

    def test_the_volatile_metadata_set_is_pinned(self):
        self.assertEqual(set(VOLATILE_METADATA), {
            "generated_at", "invocation_id", "invocation_started_at",
            "run_started_at", "user_id"})

    def test_a_non_object_has_no_identity(self):
        for value in (None, [], "manifest", 7):
            self.assertIsNone(semantic_manifest_hash(value))

    def test_a_manifest_without_metadata_is_accepted(self):
        self.assertIsNotNone(semantic_manifest_hash({"nodes": {}}))


class RealCompileTests(unittest.TestCase):
    """The two real compiles of one commit."""

    def setUp(self):
        self.a = _real("base-compile-a.json")
        self.b = _real("base-compile-b.json")

    def test_the_fixtures_really_are_different_documents(self):
        self.assertNotEqual(self.a, self.b)

    def test_two_compiles_of_one_commit_share_one_identity(self):
        self.assertEqual(semantic_manifest_hash(self.a),
                         semantic_manifest_hash(self.b))

    def test_the_raw_hashes_do_not(self):
        """Why the raw hash could never have served as the identity."""
        from agent.metadata_evidence.collection_plan import manifest_hash

        self.assertNotEqual(manifest_hash(self.a), manifest_hash(self.b))

    def test_a_real_semantic_change_is_still_visible(self):
        changed = copy.deepcopy(self.a)
        node = next(iter(changed["nodes"].values()))
        node["raw_code"] = str(node.get("raw_code", "")) + "\n-- where 1=0"
        self.assertNotEqual(semantic_manifest_hash(changed),
                            semantic_manifest_hash(self.a))


class StoredRowTests(unittest.TestCase):
    """How an already-persisted row's identity is resolved."""

    def test_a_current_row_is_trusted_as_stored(self):
        row = {"semantic_manifest_hash": "f" * 64,
               "canonicalization_version": CANONICALIZATION_VERSION,
               "manifest": _manifest()}
        self.assertEqual(stored_semantic_hash(row), "f" * 64)

    def test_a_legacy_row_is_re_derived_from_its_manifest(self):
        """The un-poisoning step.

        A row written before this module existed carries no hash at all, and
        must still resolve to the identity of the manifest it holds -- without
        being rewritten, because the table is immutable by trigger.
        """
        manifest = _manifest()
        row = {"semantic_manifest_hash": None,
               "canonicalization_version": LEGACY_CANONICALIZATION_VERSION,
               "manifest": manifest}
        self.assertEqual(stored_semantic_hash(row),
                         semantic_manifest_hash(manifest))

    def test_a_row_from_an_older_recipe_is_re_derived(self):
        """A future bump of CANONICALIZATION_VERSION must not poison rows
        written under the previous one."""
        manifest = _manifest()
        row = {"semantic_manifest_hash": "a" * 64,
               "canonicalization_version": CANONICALIZATION_VERSION - 1,
               "manifest": manifest}
        self.assertEqual(stored_semantic_hash(row),
                         semantic_manifest_hash(manifest))

    def test_a_missing_row_has_no_identity(self):
        self.assertIsNone(stored_semantic_hash(None))


class WorkflowParityTests(unittest.TestCase):
    """The workflow keeps normalising client-side, as defence in depth.

    It cannot import this module -- it is a standalone heredoc running in the
    customer's CI -- so the two definitions are pinned equal here instead.
    """

    @classmethod
    def setUpClass(cls):
        lines = [line[10:] if line.startswith(" " * 10) else line
                 for line in WORKFLOW.read_text(encoding="utf-8").splitlines()]
        start = next(i for i, line in enumerate(lines)
                     if line.startswith("VOLATILE_METADATA"))
        end = next(i for i, line in enumerate(lines)
                   if i > start and line.strip() == "return manifest")
        namespace = {}
        exec(compile("\n".join(lines[start:end + 1]), "workflow", "exec"),
             namespace)
        cls.workflow = namespace

    def test_the_volatile_metadata_sets_agree(self):
        self.assertEqual(set(self.workflow["VOLATILE_METADATA"]),
                         set(VOLATILE_METADATA))

    def test_the_volatile_entry_field_sets_agree(self):
        self.assertEqual(set(self.workflow["VOLATILE_ENTRY_FIELDS"]),
                         set(VOLATILE_ENTRY_FIELDS))

    def test_the_swept_sections_agree(self):
        self.assertEqual(set(self.workflow["ENTRY_SECTIONS"]),
                         set(ENTRY_SECTIONS))

    def test_both_produce_the_same_document_for_a_real_manifest(self):
        real = _real("base-compile-a.json")
        self.assertEqual(self.workflow["stable"](copy.deepcopy(real)),
                         canonical_manifest(real))


if __name__ == "__main__":
    unittest.main()
