"""What makes two dbt manifests the SAME evidence.

A manifest is submitted twice for one commit all the time: a workflow is
re-run, a second pull request opens from an already-analysed base, a webhook
redelivers. Every one of those is the same evidence and must reuse the row
that already exists.

Before this module the answer was a hash of the bytes the client happened to
send (``payload_hash``, and ``manifest_hash`` from ``collection_plan``), and
the only thing that removed dbt's per-run stamps was the CI workflow. So the
identity of an evidence row depended on WHICH CLIENT VERSION wrote it first:
a row written by an older workflow, or by the webhook path reading a committed
``target/manifest.json``, carried an identity derived under different rules.
A later, correctly normalised submission for the same commit then looked like
different evidence -- permanently, because the idempotency key is a pure
function of repository and SHA and the row is immutable by database trigger.

Deriving identity on the server fixes that for every writer at once, including
the ones already deployed in customer repositories. The rules below are the
same ones the CI workflow applies; ``test_manifest_identity.py`` pins the two
definitions together so they cannot drift.
"""
from __future__ import annotations

import hashlib
import json

#: Bumped when the rules below change. Stored on every row so a row written
#: under an older recipe is re-derived from its stored manifest rather than
#: compared against a hash that no longer means the same thing.
CANONICALIZATION_VERSION = 2

#: Recorded for rows written before this module existed. They carry no
#: semantic hash at all, so it is always re-derived.
LEGACY_CANONICALIZATION_VERSION = 1

#: Fields dbt stamps afresh on every compile. They describe the RUN, not the
#: code. Verified unused by the review path: what Relium reads from a node is
#: unique_id, name, resource_type, database, schema, alias, identifier,
#: depends_on and the SQL; from metadata it reads only project_name and
#: dbt_version (agent/dbt_context.py).
VOLATILE_METADATA = ("generated_at", "invocation_id", "invocation_started_at",
                     "run_started_at", "user_id")

#: Per-entry parse timestamp, present on every node and every macro. This,
#: not the metadata block, is what dominates the difference between two
#: compiles of one commit.
VOLATILE_ENTRY_FIELDS = ("created_at",)

#: Only these sections are swept for per-entry timestamps. Named explicitly
#: rather than walking the whole document, so the rule cannot silently start
#: stripping fields somewhere unexamined.
ENTRY_SECTIONS = ("nodes", "macros")


def _without_entry_timestamps(section):
    """One manifest section with per-entry parse stamps removed."""
    cleaned = {}
    for name, entry in section.items():
        if isinstance(entry, dict):
            entry = {key: value for key, value in entry.items()
                     if key not in VOLATILE_ENTRY_FIELDS}
        cleaned[name] = entry
    return cleaned


def canonical_manifest(manifest):
    """The manifest as a function of the commit, not of the run.

    Removes exactly the fields named above and nothing else. Every semantic
    field -- models, sources, columns, depends_on, SQL -- is left untouched,
    so a real change to the project still changes this document and is still
    reported as a conflict. The caller's manifest is never mutated.
    """
    if not isinstance(manifest, dict):
        return manifest

    canonical = dict(manifest)

    metadata = canonical.get("metadata")
    if isinstance(metadata, dict):
        canonical["metadata"] = {key: value for key, value in metadata.items()
                                 if key not in VOLATILE_METADATA}

    for name in ENTRY_SECTIONS:
        section = canonical.get(name)
        if isinstance(section, dict):
            canonical[name] = _without_entry_timestamps(section)

    return canonical


def semantic_manifest_hash(manifest) -> str | None:
    """Content hash of what the manifest MEANS, stable across compiles.

    ``None`` for a non-object, mirroring ``collection_plan.manifest_hash``:
    a caller that cannot produce a manifest must not be handed a hash that
    looks like agreement.
    """
    if not isinstance(manifest, dict):
        return None
    payload = json.dumps(canonical_manifest(manifest), sort_keys=True,
                         separators=(",", ":"), default=str).encode()
    return hashlib.sha256(payload).hexdigest()


def stored_semantic_hash(row) -> str | None:
    """The identity of an already-stored row under the CURRENT recipe.

    A row whose recorded recipe is the current one is trusted as it stands.
    Anything else -- a legacy row with no hash, or one written under an older
    recipe -- is re-derived from the manifest the row already holds.

    Nothing is written back. ``manifest_evidence`` is immutable by database
    trigger, and the stored manifest is the evidence; the hash is only an
    index into it. Re-deriving rather than rewriting is what stops a legacy
    row from permanently poisoning its commit SHA, and it is also what keeps
    a future bump of ``CANONICALIZATION_VERSION`` from doing so again.
    """
    if row is None:
        return None
    if (row.get("canonicalization_version") == CANONICALIZATION_VERSION
            and row.get("semantic_manifest_hash")):
        return row["semantic_manifest_hash"]
    return semantic_manifest_hash(row.get("manifest"))
