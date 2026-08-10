"""Live proof for SQL semantic Before/After evidence.

One chain, twice, with two different evidence shapes::

    real fixture SQL -> real dbt parse -> genuine unmerged fixture PR
      -> served webhook runner -> PostgreSQL review_attempts.semantic_evidence
      -> WAITING_FOR_METADATA -> authenticated public review API

What this driver owns ends at persisted semantic evidence. It does NOT drive
the collector, the metadata snapshot, worker recomputation, the final
ALLOW/WARN/BLOCK verdict, or GitHub publication -- the metadata-review E2E
already proves those, and waiting for a decision here would fail this run on
a stage it does not own.

So the two cases are two evidence shapes, not two verdicts. The `block`
fixture removes a real refund dependency and must yield an expression change
plus a join removal; the `allow` fixture changes a filter in an unrelated
model. Both must reach WAITING_FOR_METADATA with durable evidence. Neither
claims a decision: at that stage `decision` is legitimately NULL, and
inventing one would be the fabrication this E2E exists to rule out.

That evidence is not automatically a finding is proven locally instead, in
test_real_fixture_regressions, against the same real fixture SQL: the refund
change yields BLOCK/65 and the filter change yields ALLOW/100.

Nothing here authors a manifest or a SemanticDiff. The fixtures are text
mutations of the fixture repository's own SQL; dbt parses whatever they
produce and the engine decides what changed. Assertions are presence-based,
because pinning an exact change count would make a truthful extra observation
look like a regression.

The promoted frontend is not present in the repository that runs this
workflow, so the browser leg is recorded as NOT_RUN rather than fabricated --
the same rule blast_radius_e2e follows.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import signal
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import live_flow as lf  # noqa: E402
import verify_flow as vf  # noqa: E402
from live_flow import StageFailure  # noqa: E402
from metadata_review_e2e import app_jwt, gh  # noqa: E402

import dbt_fixture_project as dfp  # noqa: E402
import semantic_fixtures as sf  # noqa: E402


REPO = os.environ.get("RELIUM_E2E_REPOSITORY", "AbhishekhPR/relium-e2e-dbt")
APP_SLUG = os.environ.get("RELIUM_E2E_APP_SLUG", "relium-e2e")
FIXTURE_TOKEN = os.environ.get("RELIUM_E2E_FIXTURE_TOKEN", "")
RUN = os.environ.get("RELIUM_E2E_RUN_ID", uuid.uuid4().hex[:10])
EV = Path(sys.argv[1] if len(sys.argv) > 1 else "semantic-diff-evidence")
CLEANUP_ONLY = "--cleanup-only" in sys.argv
EV.mkdir(parents=True, exist_ok=True)
RUN_RECOVERY = EV / "semantic-diff-recovery.json"

# Importing the shared auth helper constructs the metadata driver's tracker.
# That tracker describes no run of this driver and must never be uploaded.
_STRAY_TRACKER = EV / "stage-tracker.json"
if _STRAY_TRACKER.exists():
    _STRAY_TRACKER.unlink()

#: Ordered. Cleanup and every ownership check depend on this exact order.
CASES = ("block", "allow")

state = {"procs": [], "tunnel": None, "cleanup_done": False,
         "cleanup_result": None, "expected_slug": APP_SLUG, "mutated": False}

#: Indirection so fault-injection tests can substitute a recording adapter
#: without the harness knowing. Production paths never reassign these.
GH = gh
APP_JWT = app_jwt


def _write(name: str, document) -> None:
    (EV / name).write_text(
        json.dumps(document, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8")


def _write_recovery(record) -> None:
    """Atomically replace the recovery record.

    Cleanup after a crash can only remove what this file proves is owned, so
    a torn write would strand a real remote artifact.
    """
    payload = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = RUN_RECOVERY.with_name(f".{RUN_RECOVERY.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, RUN_RECOVERY)
        try:
            directory_fd = os.open(str(RUN_RECOVERY.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Directory handles are not fsync-able on every supported OS. The
            # file write and atomic replace are still mandatory.
            pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _load_recovery() -> dict | None:
    if not RUN_RECOVERY.is_file():
        return None
    try:
        record = json.loads(RUN_RECOVERY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StageFailure(f"cannot read semantic recovery record: {type(exc).__name__}")
    return record if isinstance(record, dict) else None


# -- ownership -------------------------------------------------------------
#
# Four refs and two pull requests. Ownership is positional: a ref is owned
# only if it is the next candidate in this exact order and its creation was
# recorded durably before the mutating call. Nothing is ever removed because
# its name merely looks like an E2E artifact.

def expected_branches(run_id: str) -> list[str]:
    return [f"e2e/semantic-{case}-{role}-{run_id}"
            for case in CASES for role in ("base", "head")]


def _initial_recovery() -> dict:
    record = {
        "run_id": RUN,
        "repository": REPO,
        "expected_app_slug": APP_SLUG,
        "webhook_preserved": False,
        "webhook_mutated": False,
        "original_webhook": None,
        "branch_candidates": expected_branches(RUN),
        "owned_branches": [],
        "owned_branch_heads": {},
        "branch_mutation_intents": [],
        "branch_head_mutation_intents": [],
        "pull_candidates": list(CASES),
        "owned_pulls": [],
        "pull_mutation_intents": [],
        "processes": [],
        "database": {"export_path": None, "restored": None},
    }
    _write_recovery(record)
    state["expected_slug"] = record["expected_app_slug"]
    state["mutated"] = False
    return record


def _validate_record(record: dict) -> tuple[list[str], list[str], list[dict]]:
    """Reject any record that does not describe exactly this run's artifacts.

    Every cleanup decision is made from this record, so a malformed record
    must stop cleanup rather than let it guess.
    """
    run_id = record.get("run_id")
    if (not isinstance(run_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{6,32}", run_id) is None):
        raise StageFailure("semantic ownership record has an invalid run id")
    candidates = expected_branches(run_id)
    if record.get("branch_candidates") != candidates:
        raise StageFailure("branch candidate record must be the exact ordered ref set")
    owned = record.get("owned_branches")
    if not isinstance(owned, list) or owned != candidates[:len(owned)]:
        raise StageFailure("owned branches must be an ordered prefix of the exact refs")
    heads = record.get("owned_branch_heads")
    if (not isinstance(heads, dict) or set(heads) != set(owned)
            or any(not isinstance(heads[branch], str) or not heads[branch]
                   for branch in owned)):
        raise StageFailure("owned branch head record must exactly map every owned ref")

    intents = record.get("branch_mutation_intents")
    if not isinstance(intents, list) or len(intents) > 1:
        raise StageFailure("branch mutation intent record must contain at most one intent")
    if intents:
        intent = intents[0]
        next_branch = candidates[len(owned)] if len(owned) < len(candidates) else None
        if (not isinstance(intent, dict) or intent.get("branch") != next_branch
                or intent.get("ref") != f"refs/heads/{next_branch}"
                or not isinstance(intent.get("expected_sha"), str)
                or not intent["expected_sha"]):
            raise StageFailure("branch mutation intent is not the exact next candidate")

    head_intents = record.get("branch_head_mutation_intents")
    if not isinstance(head_intents, list) or len(head_intents) > 1:
        raise StageFailure("branch head intent record must contain at most one intent")
    if head_intents:
        intent = head_intents[0]
        branch = intent.get("branch") if isinstance(intent, dict) else None
        operation = intent.get("operation_identity") if isinstance(intent, dict) else None
        if (intents or branch not in owned
                or intent.get("ref") != f"refs/heads/{branch}"
                or intent.get("old_sha") != heads.get(branch)
                or not isinstance(intent.get("new_sha"), str) or not intent["new_sha"]
                or intent["new_sha"] == intent["old_sha"]
                or not isinstance(operation, dict)
                or operation.get("kind") != "git-data-file-commit"
                or not isinstance(operation.get("path"), str) or not operation["path"]
                or not isinstance(operation.get("blob_sha"), str)
                or not operation["blob_sha"]):
            raise StageFailure("branch head mutation intent is not exactly typed")

    pulls = record.get("owned_pulls")
    if not isinstance(pulls, list) or len(pulls) > len(CASES):
        raise StageFailure("owned pull record must not exceed the exact case count")
    for position, pull in enumerate(pulls):
        if (not isinstance(pull, dict) or pull.get("case") != CASES[position]
                or not isinstance(pull.get("number"), int)
                or pull["number"] <= 0
                or pull.get("head") not in owned or pull.get("base") not in owned):
            raise StageFailure("owned pull record is not exactly typed and owned")
    pull_intents = record.get("pull_mutation_intents")
    if not isinstance(pull_intents, list) or len(pull_intents) > 1:
        raise StageFailure("pull mutation intent record must contain at most one intent")
    if pull_intents:
        intent = pull_intents[0]
        next_case = CASES[len(pulls)] if len(pulls) < len(CASES) else None
        if (not isinstance(intent, dict) or intent.get("case") != next_case
                or intent.get("head") not in owned or intent.get("base") not in owned
                or intent.get("head") == intent.get("base")):
            raise StageFailure("pull mutation intent is not the exact next owned case")
    return candidates, owned, pulls


def case_branches(run_id: str, case: str) -> tuple[str, str]:
    return (f"e2e/semantic-{case}-base-{run_id}",
            f"e2e/semantic-{case}-head-{run_id}")


# -- expectations ----------------------------------------------------------
#
# Presence-based. An exact change count would turn a truthful extra
# observation into a false failure, and the real fixture emits four.

REQUIRED_BLOCK_EVIDENCE = (
    {"kind": "projection_expression_changed", "output_name": "net_order_amount"},
    {"kind": "join_removed", "relation": "int_order_refunds"},
)


def _matches(change: dict, required: dict) -> bool:
    return all(change.get(key) == value for key, value in required.items())


def semantic_changes(evidence: dict | None) -> list[dict]:
    """Every change in an evidence document, whichever shape it arrives in.

    The runner groups changes under `models[]`; the public API projection
    flattens them into `changes[]`. The harness reads the API, but accepting
    both matters: silently finding nothing in the other shape would look
    exactly like "the comparison ran and found no changes", which is the one
    conclusion this E2E must never reach by accident.
    """
    if not isinstance(evidence, dict):
        return []
    flat = evidence.get("changes")
    if isinstance(flat, list):
        return [change for change in flat if isinstance(change, dict)]
    collected = []
    for model in evidence.get("models") or []:
        if not isinstance(model, dict):
            continue
        for change in model.get("changes") or []:
            if isinstance(change, dict):
                # Model identity lives on the group, not the change.
                merged = dict(change)
                merged.setdefault("model_name", model.get("model_name"))
                collected.append(merged)
    return collected


def assert_block_expectations(incident: dict, evidence: dict) -> dict:
    """The refund removal: evidence AND the existing policy outcome.

    IN-MEMORY ONLY. `material_sql_changes` lives on the incident that
    `review_manifest_change` returns and is never persisted -- confirmed by
    inspecting a real attempt row, whose payload carries findings and plan
    only. Do not point this at a PostgreSQL row: it would look for a
    `metadata.manifest_comparison` path that does not exist there and report
    a missing refund signal for a perfectly good review. The remote driver
    uses `assert_remote_semantic_evidence` instead.
    """
    failures = []
    changes = semantic_changes(evidence)
    if (evidence or {}).get("status") != "evaluated":
        failures.append(f"semantic status is {(evidence or {}).get('status')!r}, not evaluated")
    for required in REQUIRED_BLOCK_EVIDENCE:
        if not any(_matches(change, required) for change in changes):
            failures.append(f"required semantic evidence missing: {required}")
    if incident.get("decision") != "BLOCK":
        failures.append(f"decision is {incident.get('decision')!r}, not BLOCK")
    if incident.get("health") != 65:
        failures.append(f"health is {incident.get('health')!r}, not 65")
    material = ((incident.get("metadata") or {}).get("manifest_comparison")
                or {}).get("material_sql_changes") or []
    if not material:
        failures.append("material refund signal is absent")
    unexplained = _unexplained(changes, sf.BLOCK_MUTATED_MODELS)
    if unexplained:
        failures.append(f"semantic changes not backed by the mutation: {unexplained}")
    return {"case": "block", "passed": not failures, "failures": failures,
            "change_kinds": sorted({c.get("kind") for c in changes}),
            "change_count": len(changes), "decision": incident.get("decision"),
            "health": incident.get("health"), "material_sql_changes": len(material)}


def assert_allow_expectations(incident: dict, evidence: dict) -> dict:
    """A real SQL change that no policy consumes: evidence is not a finding."""
    failures = []
    changes = semantic_changes(evidence)
    if (evidence or {}).get("status") != "evaluated":
        failures.append(f"semantic status is {(evidence or {}).get('status')!r}, not evaluated")
    if not changes:
        failures.append("semantic evidence is empty; the fixture proved nothing")
    if incident.get("decision") != "ALLOW":
        failures.append(f"decision is {incident.get('decision')!r}, not ALLOW")
    if incident.get("health") != 100:
        failures.append(f"health is {incident.get('health')!r}, not 100")
    material = ((incident.get("metadata") or {}).get("manifest_comparison")
                or {}).get("material_sql_changes") or []
    if material:
        failures.append(f"material_sql_changes is {len(material)}, not 0")
    unexplained = _unexplained(changes, sf.ALLOW_MUTATED_MODELS)
    if unexplained:
        failures.append(f"semantic changes not backed by the mutation: {unexplained}")
    return {"case": "allow", "passed": not failures, "failures": failures,
            "change_kinds": sorted({c.get("kind") for c in changes}),
            "change_count": len(changes), "decision": incident.get("decision"),
            "health": incident.get("health"), "material_sql_changes": len(material)}


def _unexplained(changes: list[dict], mutated_models: tuple[str, ...]) -> list[str]:
    """Extra evidence is welcome, but only about models the fixture edited.

    This is what makes "do not assert an exact count" safe: additional
    truthful changes pass, while a change attributed to an untouched model
    fails, because the fixture cannot explain it.
    """
    return sorted({str(change.get("model_name")) for change in changes
                   if change.get("model_name") not in mutated_models})


# -- API and UI assertion plan --------------------------------------------

def plan_ui_assertions(api_payload: dict) -> dict:
    """What the browser leg must compare, derived from the API payload.

    Every expected string comes from the payload the backend actually
    returned. Nothing here is demo text: if the API says nothing, the plan
    contains nothing to assert, and the browser leg fails rather than
    matching a hardcoded fixture.
    """
    evidence = ((api_payload or {}).get("semantic_evidence") or {})
    changes = semantic_changes(evidence)
    expected = []
    for change in changes:
        kind = change.get("kind")
        if kind == "projection_expression_changed":
            expected.append({"concept": "expression_before_after",
                             "before": change.get("before_sql"),
                             "after": change.get("after_sql"),
                             "label": change.get("output_name")})
        elif kind == "join_removed":
            expected.append({"concept": "join_removed",
                             "before": change.get("before_condition_sql"),
                             "join_type": change.get("before_join_type"),
                             "label": change.get("relation")})
        else:
            expected.append({"concept": "additional_truthful_change",
                             "kind": kind, "label": change.get("model_name")})
    return {
        "source": "public_api_semantic_evidence",
        "expected_cards": expected,
        "invariants": [
            "semantic cards are rendered separately from findings",
            "the decision badge is rendered separately from semantic evidence",
            "blast radius remains its own section",
            "no speculative business-impact prose accompanies a change",
        ],
        # Recorded, never launched here: this driver must not touch remote
        # state, and the promoted frontend is not in this repository.
        "browser": "NOT_RUN",
    }


# -- webhook ---------------------------------------------------------------

def preserve_webhook() -> dict:
    record = _load_recovery()
    if record is None:
        raise StageFailure("recovery record must exist before webhook preservation")
    status, hook = GH("GET", "/app/hook/config", APP_JWT())
    if status != 200:
        raise StageFailure(f"cannot preserve webhook: HTTP {status}")
    if "insecure_ssl" not in hook:
        raise StageFailure("webhook preservation returned no TLS configuration")
    original = {"url": hook.get("url"),
                "content_type": hook.get("content_type", "json"),
                "insecure_ssl": hook.get("insecure_ssl")}
    if not original["url"]:
        raise StageFailure("webhook preservation returned no URL")
    record["original_webhook"] = original
    record["webhook_preserved"] = True
    _write_recovery(record)
    return {"preserved": True, "secret_captured": False,
            "url_host": original["url"].split("//")[-1]}


def restore_webhook(record: dict) -> dict:
    original = record.get("original_webhook")
    if not record.get("webhook_preserved") or not isinstance(original, dict):
        return {"restored": False, "verified_through_github": False,
                "reason": "original webhook was not durably preserved"}
    jwt = APP_JWT()
    patch_status, _ = GH(
        "PATCH", "/app/hook/config", jwt,
        {"url": original.get("url"), "content_type": original.get("content_type"),
         "insecure_ssl": original.get("insecure_ssl")})
    get_status, confirmed = GH("GET", "/app/hook/config", jwt)
    url_matches = get_status == 200 and confirmed.get("url") == original.get("url")
    type_matches = (get_status == 200
                    and confirmed.get("content_type") == original.get("content_type"))
    ssl_matches = (get_status == 200
                   and confirmed.get("insecure_ssl") == original.get("insecure_ssl"))
    verified = patch_status == 200 and url_matches and type_matches and ssl_matches
    return {"restored": patch_status == 200, "patch_status": patch_status,
            "get_status": get_status, "url_matches_original": url_matches,
            "content_type_matches_original": type_matches,
            "insecure_ssl_matches_original": ssl_matches,
            "verified_through_github": verified, "secret_touched": False}


# -- owned remote artifacts -----------------------------------------------

def make_branch(branch: str, from_sha: str) -> None:
    """Create the next candidate ref, recording intent before the call."""
    record = _load_recovery()
    if record is None:
        raise StageFailure("branch creation has no recovery record")
    candidates, owned, _pulls = _validate_record(record)
    if len(owned) >= len(candidates) or branch != candidates[len(owned)]:
        raise StageFailure("branch creation is not the next exact owned candidate")
    preflight_status, _ = GH(
        "GET", f"/repos/{REPO}/git/ref/heads/{branch}", FIXTURE_TOKEN, bearer=False)
    if preflight_status != 404:
        raise StageFailure(f"owned branch candidate {branch} was not absent at preflight")
    intent = {"branch": branch, "ref": f"refs/heads/{branch}",
              "expected_sha": from_sha}
    record["branch_mutation_intents"] = [intent]
    _write_recovery(record)  # durable BEFORE the mutating GitHub call
    status, _ = GH("POST", f"/repos/{REPO}/git/refs", FIXTURE_TOKEN,
                   {"ref": f"refs/heads/{branch}", "sha": from_sha}, bearer=False)
    if status != 201:
        raise StageFailure(f"cannot create owned branch {branch}: HTTP {status}")
    record = _load_recovery()
    _validate_record(record)
    if record.get("branch_mutation_intents") != [intent]:
        raise StageFailure("owned branch intent changed during creation")
    record["owned_branches"] = owned + [branch]
    record["owned_branch_heads"][branch] = from_sha
    record["branch_mutation_intents"] = []
    _write_recovery(record)


def commit_file(branch: str, path: str, content: str, message: str) -> str:
    """Commit one file to an owned branch through the Git Data API."""
    record = _load_recovery()
    if record is None:
        raise StageFailure("cannot commit to owned branch without recovery")
    _candidates, owned, _pulls = _validate_record(record)
    if branch not in owned:
        raise StageFailure("cannot commit to a branch not proven owned")
    old_sha = record["owned_branch_heads"][branch]
    status, parent = GH("GET", f"/repos/{REPO}/git/commits/{old_sha}",
                        FIXTURE_TOKEN, bearer=False)
    parent_tree = ((parent.get("tree") or {}).get("sha")
                   if isinstance(parent, dict) else None)
    if status != 200 or parent.get("sha") != old_sha or not parent_tree:
        raise StageFailure("owned branch parent commit was not exactly readable")
    import base64 as _b64
    status, blob = GH(
        "POST", f"/repos/{REPO}/git/blobs", FIXTURE_TOKEN,
        {"content": _b64.b64encode(content.encode("utf-8")).decode("ascii"),
         "encoding": "base64"}, bearer=False)
    blob_sha = blob.get("sha") if isinstance(blob, dict) else None
    if status != 201 or not isinstance(blob_sha, str) or not blob_sha:
        raise StageFailure("Git Data blob creation did not return a typed SHA")
    status, tree = GH(
        "POST", f"/repos/{REPO}/git/trees", FIXTURE_TOKEN,
        {"base_tree": parent_tree,
         "tree": [{"path": path, "mode": "100644", "type": "blob",
                   "sha": blob_sha}]}, bearer=False)
    tree_sha = tree.get("sha") if isinstance(tree, dict) else None
    if status != 201 or not isinstance(tree_sha, str) or not tree_sha:
        raise StageFailure("Git Data tree creation did not return a typed SHA")
    status, commit = GH(
        "POST", f"/repos/{REPO}/git/commits", FIXTURE_TOKEN,
        {"message": message, "tree": tree_sha, "parents": [old_sha]}, bearer=False)
    new_sha = commit.get("sha") if isinstance(commit, dict) else None
    if (status != 201 or not isinstance(new_sha, str) or not new_sha
            or new_sha == old_sha):
        raise StageFailure("Git Data commit creation did not return a new typed SHA")
    intent = {"branch": branch, "ref": f"refs/heads/{branch}", "old_sha": old_sha,
              "new_sha": new_sha,
              "operation_identity": {"kind": "git-data-file-commit", "path": path,
                                     "blob_sha": blob_sha}}
    record = _load_recovery()
    _validate_record(record)
    if record["owned_branch_heads"].get(branch) != old_sha:
        raise StageFailure("owned branch head changed before mutation intent")
    record["branch_head_mutation_intents"] = [intent]
    _write_recovery(record)  # durable BEFORE the ref-moving PATCH
    patch_status, _ = GH("PATCH", f"/repos/{REPO}/git/refs/heads/{branch}",
                         FIXTURE_TOKEN, {"sha": new_sha, "force": False}, bearer=False)
    if patch_status != 200:
        raise StageFailure(f"owned branch ref update returned HTTP {patch_status}")
    record = _load_recovery()
    _validate_record(record)
    if record.get("branch_head_mutation_intents") != [intent]:
        raise StageFailure("branch head mutation intent changed during update")
    record["owned_branch_heads"][branch] = new_sha
    record["branch_head_mutation_intents"] = []
    _write_recovery(record)
    return new_sha


def open_pull(case: str, head: str, base: str, title: str, body: str) -> int:
    """Open the fixture PR for one case, recording intent before the call."""
    record = _load_recovery()
    if record is None:
        raise StageFailure("pull creation has no recovery record")
    _candidates, owned, pulls = _validate_record(record)
    if len(pulls) >= len(CASES) or case != CASES[len(pulls)]:
        raise StageFailure("pull creation is not the next exact owned case")
    if head not in owned or base not in owned:
        raise StageFailure("pull creation requires both refs proven owned")
    intent = {"case": case, "head": head, "base": base}
    record["pull_mutation_intents"] = [intent]
    _write_recovery(record)  # durable BEFORE the mutating GitHub call
    status, pull = GH("POST", f"/repos/{REPO}/pulls", FIXTURE_TOKEN,
                      {"title": title, "head": head, "base": base, "body": body},
                      bearer=False)
    number = pull.get("number") if isinstance(pull, dict) else None
    if status != 201 or not isinstance(number, int) or number <= 0:
        raise StageFailure(f"cannot open owned fixture PR for {case}: HTTP {status}")
    record = _load_recovery()
    _validate_record(record)
    if record.get("pull_mutation_intents") != [intent]:
        raise StageFailure("pull mutation intent changed during creation")
    record["owned_pulls"] = pulls + [
        {"case": case, "number": number, "head": head, "base": base}]
    record["pull_mutation_intents"] = []
    _write_recovery(record)
    return number


def _validate_owned_pull(pull: dict, owned_branches: list[str]) -> dict:
    """Refuse to act on a PR that is not exactly one of ours.

    A PR number alone is not ownership: the head and base must both be refs
    this run created, or closing it would close somebody else's work.
    """
    if not isinstance(pull, dict):
        raise StageFailure("owned pull response is not an object")
    head = ((pull.get("head") or {}).get("ref")
            if isinstance(pull.get("head"), dict) else None)
    base = ((pull.get("base") or {}).get("ref")
            if isinstance(pull.get("base"), dict) else None)
    if head not in owned_branches or base not in owned_branches:
        raise StageFailure(
            f"pull #{pull.get('number')} is not between two owned refs")
    return pull


# -- cleanup ---------------------------------------------------------------
#
# Written before the happy path and callable after a failure at any stage.
# Nothing reports success that was not independently re-read from GitHub.

def _resolve_branch_intent(record: dict, failures: list[str]) -> dict:
    """Adopt or disown a ref whose creation was interrupted."""
    intents = record.get("branch_mutation_intents") or []
    if not intents:
        return record
    intent = intents[0]
    branch = intent["branch"]
    status, ref = GH("GET", f"/repos/{REPO}/git/ref/heads/{branch}",
                     FIXTURE_TOKEN, bearer=False)
    if status == 404:
        # The mutation never landed. Nothing is owned; drop the intent.
        record["branch_mutation_intents"] = []
        _write_recovery(record)
        return record
    if status != 200:
        failures.append(f"cannot resolve interrupted branch {branch}: HTTP {status}")
        return record
    actual = ((ref.get("object") or {}).get("sha")
              if isinstance(ref, dict) else None)
    if actual != intent.get("expected_sha"):
        # It exists but is not what this run would have created. Refuse to
        # claim it -- deleting it could destroy somebody else's ref.
        failures.append(
            f"branch {branch} exists at an unexpected SHA; not claiming ownership")
        record["branch_mutation_intents"] = []
        _write_recovery(record)
        return record
    record["owned_branches"] = list(record.get("owned_branches") or []) + [branch]
    record["owned_branch_heads"][branch] = actual
    record["branch_mutation_intents"] = []
    _write_recovery(record)
    return record


def _resolve_head_intent(record: dict, failures: list[str]) -> dict:
    """Reconcile a commit whose ref update may or may not have landed."""
    intents = record.get("branch_head_mutation_intents") or []
    if not intents:
        return record
    intent = intents[0]
    branch = intent["branch"]
    status, ref = GH("GET", f"/repos/{REPO}/git/ref/heads/{branch}",
                     FIXTURE_TOKEN, bearer=False)
    if status == 404:
        failures.append(f"owned branch {branch} vanished during head reconciliation")
        record["branch_head_mutation_intents"] = []
        _write_recovery(record)
        return record
    if status != 200:
        failures.append(f"cannot reconcile head of {branch}: HTTP {status}")
        return record
    actual = ((ref.get("object") or {}).get("sha")
              if isinstance(ref, dict) else None)
    if actual in (intent.get("old_sha"), intent.get("new_sha")):
        # Either outcome is ours and both are deletable; record the truth.
        record["owned_branch_heads"][branch] = actual
    else:
        failures.append(
            f"owned branch {branch} head is neither the recorded old nor new SHA")
    record["branch_head_mutation_intents"] = []
    _write_recovery(record)
    return record


def _resolve_pull_intent(record: dict, failures: list[str]) -> dict:
    """Find a PR whose creation was interrupted, by its owned head ref."""
    intents = record.get("pull_mutation_intents") or []
    if not intents:
        return record
    intent = intents[0]
    owned = list(record.get("owned_branches") or [])
    status, pulls = GH(
        "GET", f"/repos/{REPO}/pulls?state=all&head={OWNER}:{intent['head']}",
        FIXTURE_TOKEN, bearer=False)
    if status != 200 or not isinstance(pulls, list):
        failures.append(
            f"cannot resolve interrupted pull for {intent['case']}: HTTP {status}")
        return record
    matching = [p for p in pulls
                if ((p.get("head") or {}).get("ref") == intent["head"]
                    and (p.get("base") or {}).get("ref") == intent["base"])]
    if not matching:
        record["pull_mutation_intents"] = []
        _write_recovery(record)
        return record
    if len(matching) > 1:
        failures.append(
            f"interrupted pull for {intent['case']} matched {len(matching)} PRs")
        return record
    pull = _validate_owned_pull(matching[0], owned)
    record["owned_pulls"] = list(record.get("owned_pulls") or []) + [
        {"case": intent["case"], "number": pull["number"],
         "head": intent["head"], "base": intent["base"]}]
    record["pull_mutation_intents"] = []
    _write_recovery(record)
    return record


def _close_owned_pulls(record: dict, owned: list[str], result: dict) -> bool:
    """Close every owned PR unmerged. Returns whether refs are safe to delete."""
    all_closed = True
    for entry in list(record.get("owned_pulls") or []):
        number = entry["number"]
        try:
            status, pull = GH("GET", f"/repos/{REPO}/pulls/{number}",
                              FIXTURE_TOKEN, bearer=False)
            if status != 200:
                raise StageFailure(f"cannot read owned fixture PR #{number}: HTTP {status}")
            pull = _validate_owned_pull(pull, owned)
            if pull.get("merged"):
                raise StageFailure(f"owned fixture PR #{number} was MERGED")
            if pull.get("state") != "closed":
                close_status, _ = GH("PATCH", f"/repos/{REPO}/pulls/{number}",
                                     FIXTURE_TOKEN, {"state": "closed"}, bearer=False)
                if close_status != 200:
                    raise StageFailure(
                        f"cannot close owned fixture PR #{number}: HTTP {close_status}")
            verify_status, verified = GH("GET", f"/repos/{REPO}/pulls/{number}",
                                         FIXTURE_TOKEN, bearer=False)
            if verify_status != 200:
                raise StageFailure(f"owned fixture PR #{number} close was not readable")
            verified = _validate_owned_pull(verified, owned)
            ok = verified.get("state") == "closed" and not verified.get("merged")
            result["fixture_pulls"].append(
                {"case": entry["case"], "number": number,
                 "state": verified.get("state"), "merged": verified.get("merged"),
                 "verified_through_github": ok})
            if not ok:
                raise StageFailure(f"owned fixture PR #{number} close was not verified")
        except Exception as exc:  # noqa: BLE001
            all_closed = False
            result["failures"].append(str(exc))
    return all_closed


def _delete_owned_branches(owned: list[str], result: dict) -> None:
    """Remove each owned ref, deciding from a follow-up read rather than the
    DELETE status alone.

    GitHub answers a DELETE of an already-absent ref under `git/refs/heads/`
    with 422, not 404. Treating that as a failure made run 31330824658 report
    four stranded refs that were in fact gone. Blanket-accepting 422 would be
    the opposite mistake -- it would let a genuinely surviving ref pass. So an
    unexpected status decides nothing on its own: the exact ref is re-read,
    and only proven absence counts as cleaned.
    """
    for branch in owned:
        ref_path = f"/repos/{REPO}/git/ref/heads/{branch}"
        try:
            status, _ = GH("DELETE", f"/repos/{REPO}/git/refs/heads/{branch}",
                           FIXTURE_TOKEN, bearer=False)
            if status == 204:
                result["fixture_branches_deleted"].append(branch)
            elif status == 404:
                result["fixture_branches_already_absent"].append(branch)
            else:
                # Decide nothing from the status; prove it by reading the ref.
                try:
                    verify_status, _ = GH("GET", ref_path, FIXTURE_TOKEN,
                                          bearer=False)
                except Exception as exc:  # noqa: BLE001
                    result["failures"].append(
                        f"owned branch {branch} deletion returned HTTP {status} and "
                        f"the verifying read raised {type(exc).__name__}")
                    continue
                if verify_status == 404:
                    result["fixture_branches_already_absent"].append(branch)
                    result.setdefault("reconciled_deletions", []).append(
                        {"branch": branch, "delete_status": status,
                         "verified_absent_by": "GET 404"})
                    continue
                if verify_status == 200:
                    result["failures"].append(
                        f"owned branch {branch} still present after deletion "
                        f"returned HTTP {status}")
                else:
                    result["failures"].append(
                        f"owned branch {branch} deletion returned HTTP {status} and "
                        f"the verifying read was ambiguous: HTTP {verify_status}")
                continue
            verify_status, _ = GH("GET", ref_path, FIXTURE_TOKEN, bearer=False)
            if verify_status != 404:
                result["failures"].append(
                    f"owned branch {branch} still present after deletion")
        except Exception as exc:  # noqa: BLE001
            result["failures"].append(
                f"deleting owned branch {branch} raised {type(exc).__name__}")


def _remove_database_artifacts(record: dict, result: dict) -> None:
    """Remove the export and any restored temporary database."""
    database = record.get("database") or {}
    export = database.get("export_path")
    removed = []
    if export:
        try:
            path = Path(export)
            if path.is_file():
                path.unlink()
                removed.append(str(path))
            verified_absent = not path.exists()
        except OSError as exc:
            result["failures"].append(f"cannot remove DB export: {type(exc).__name__}")
            verified_absent = False
    else:
        verified_absent = True
    result["database"] = {"export_removed": removed,
                          "export_verified_absent": verified_absent,
                          "restored": database.get("restored")}


def cleanup(reason: str = "normal") -> dict:
    if state.get("cleanup_done"):
        return state.get("cleanup_result") or {}
    state["cleanup_done"] = True
    result = {"reason": reason, "failures": [], "nothing_owned": False,
              "fixture_pulls": [], "fixture_branches_deleted": [],
              "fixture_branches_already_absent": []}
    record = _load_recovery()
    if record is None:
        result.update({
            "nothing_owned": True,
            "webhook": {"restored": None, "verified_through_github": False,
                        "reason": "no recovery record; no mutation is attributed"},
            "processes": [],
            "listeners_remaining": [port for port in (lf.PORT, 5181)
                                    if _listener_up(port)]})
        if result["listeners_remaining"]:
            result["failures"].append(
                f"local E2E listeners remain: {result['listeners_remaining']}")
        result["cleanup_passed"] = not result["failures"]
        state["cleanup_result"] = result
        _write("semantic-diff-cleanup.json", result)
        return result

    owned: list[str] = []
    validated = True
    try:
        _candidates, owned, _pulls = _validate_record(record)
    except Exception as exc:  # noqa: BLE001
        validated = False
        result["failures"].append(str(exc))

    # The webhook is the only global external mutation. It is restored first,
    # before any fixture ref or local process is touched.
    if record.get("webhook_mutated"):
        try:
            result["webhook"] = restore_webhook(record)
            if not result["webhook"].get("verified_through_github"):
                result["failures"].append("webhook restoration not verified by exact GET")
        except Exception as exc:  # noqa: BLE001
            result["webhook"] = {"restored": False, "verified_through_github": False,
                                 "error": type(exc).__name__}
            result["failures"].append(f"webhook restoration raised {type(exc).__name__}")
    else:
        result["webhook"] = {"restored": None, "verified_through_github": False,
                             "reason": "record attributes no webhook mutation"}

    if validated and FIXTURE_TOKEN:
        for resolver in (_resolve_branch_intent, _resolve_head_intent,
                         _resolve_pull_intent):
            try:
                record = resolver(record, result["failures"])
            except Exception as exc:  # noqa: BLE001
                result["failures"].append(
                    f"{resolver.__name__} raised {type(exc).__name__}")
        try:
            _candidates, owned, _pulls = _validate_record(record)
        except Exception as exc:  # noqa: BLE001
            validated = False
            result["failures"].append(str(exc))
    elif validated and not FIXTURE_TOKEN:
        if record.get("owned_branches") or record.get("owned_pulls"):
            result["failures"].append("fixture token unavailable for owned cleanup")

    if validated and FIXTURE_TOKEN:
        refs_safe = _close_owned_pulls(record, owned, result)
        if refs_safe:
            _delete_owned_branches(owned, result)
        elif owned:
            result["failures"].append(
                "owned refs retained because an owned PR was not verified closed")

    # Local processes and temporary data are removed regardless of remote
    # outcome; leaving a listener or an export behind is its own failure.
    result["processes"] = _stop_processes(record, result["failures"])
    _remove_database_artifacts(record, result)
    result["listeners_remaining"] = [port for port in (lf.PORT, 5181)
                                     if _listener_up(port)]
    if result["listeners_remaining"]:
        result["failures"].append(
            f"local E2E listeners remain: {result['listeners_remaining']}")

    result["cleanup_passed"] = not result["failures"]
    state["cleanup_result"] = result
    _write("semantic-diff-cleanup.json", result)
    return result


def _listener_up(port: int) -> bool:
    import socket
    with socket.socket() as probe:
        probe.settimeout(0.4)
        try:
            probe.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _stop_processes(record: dict, failures: list[str]) -> list[dict]:
    stopped = []
    for entry in list(record.get("processes") or []):
        pid = entry.get("pid")
        if not isinstance(pid, int):
            continue
        alive_before = _process_alive(pid)
        if alive_before:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        stopped.append({"label": entry.get("label"), "pid": pid,
                        "was_running": alive_before,
                        "still_running": _process_alive(pid)})
    for entry in stopped:
        if entry["still_running"]:
            failures.append(f"process {entry['label']} (pid {entry['pid']}) survived cleanup")
    return stopped


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _persist_process(label: str, proc, marker: str) -> None:
    record = _load_recovery()
    if record is None:
        return
    record["processes"] = list(record.get("processes") or []) + [
        {"label": label, "pid": getattr(proc, "pid", None), "marker": marker}]
    _write_recovery(record)


def _arm_cleanup() -> None:
    atexit.register(cleanup, "atexit")
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda value, _frame: (
                cleanup(f"signal-{value}"), sys.exit(130)))
        except (ValueError, OSError):
            pass


# -- environment gate ------------------------------------------------------

OWNER, REPO_NAME = REPO.split("/", 1)


def _environment_gate() -> dict:
    """Refuse to run unless this is unambiguously the dedicated E2E App."""
    if not FIXTURE_TOKEN:
        raise StageFailure("fixture token is not configured")
    jwt = APP_JWT()
    status, app = GH("GET", "/app", jwt)
    slug = app.get("slug") if status == 200 else None
    if slug != APP_SLUG or "pilot" in str(slug).lower():
        raise StageFailure(f"unexpected GitHub App identity: {slug}")
    events = sorted(app.get("events") or [])
    if "pull_request" not in events:
        raise StageFailure("dedicated E2E App is not subscribed to pull_request")
    installation = __import__("metadata_review_e2e").installation_token(jwt)
    status, repositories = GH("GET", "/installation/repositories", installation,
                              bearer=False)
    names = sorted(item["full_name"] for item in
                   (repositories.get("repositories") or [])) if status == 200 else []
    if names != [REPO]:
        raise StageFailure(f"dedicated App repository scope is {names}")
    fixture_scope = lf.assert_fixture_token_scope(GH, FIXTURE_TOKEN, REPO)
    if fixture_scope.get("private_repositories_visible") != [REPO]:
        raise StageFailure("fixture token private repository scope is not exact")
    return {"app_slug": slug, "app_events": events, "app_repositories": names,
            "fixture_private_repositories":
                fixture_scope.get("private_repositories_visible")}


def _fixture_main_identity() -> tuple[str, str]:
    status, repo_info = GH("GET", f"/repos/{REPO}", FIXTURE_TOKEN, bearer=False)
    if status != 200:
        raise StageFailure(f"cannot read fixture repository: HTTP {status}")
    default_branch = repo_info.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise StageFailure("fixture repository has no default branch identity")
    status, ref = GH("GET", f"/repos/{REPO}/git/ref/heads/{default_branch}",
                     FIXTURE_TOKEN, bearer=False)
    if status != 200:
        raise StageFailure(f"cannot read fixture main ref: HTTP {status}")
    return default_branch, ref["object"]["sha"]


# -- fixture preparation ---------------------------------------------------

def prepare_case(case: str, main_files: dict[str, str]) -> dict:
    """Build base/head file maps and real manifests for one case.

    dbt parses both sides. If either parse fails the case fails: a manifest
    is never hand-authored here.
    """
    base_files = dict(main_files)
    base_files["relium.yml"] = sf.relium_config()
    if case == "block":
        head_files = sf.block_fixture_files(base_files)
        changed_path, changed_models = sf.FACT_PATH, ["fct_orders"]
    elif case == "allow":
        head_files = sf.allow_fixture_files(base_files, marker=RUN)
        changed_path, changed_models = sf.ALLOW_PATH, ["int_customer_orders"]
    else:
        raise StageFailure(f"unknown semantic fixture case {case!r}")
    if head_files[changed_path] == base_files[changed_path]:
        raise StageFailure(f"{case} fixture did not change {changed_path}")
    base_manifest = dfp.parse_manifest(base_files, prefix=f"relium-semantic-{case}-base-")
    head_manifest = dfp.parse_manifest(head_files, prefix=f"relium-semantic-{case}-head-")
    identities = {}
    for model in changed_models:
        base_id = dfp.model_identity(base_manifest, model)
        head_id = dfp.model_identity(head_manifest, model)
        if base_id != head_id:
            raise StageFailure(
                f"{case} fixture changed the identity of {model}: {base_id} -> {head_id}")
        identities[model] = base_id
    return {"case": case, "base_files": base_files, "head_files": head_files,
            "base_manifest": base_manifest, "head_manifest": head_manifest,
            "changed_path": changed_path, "changed_models": changed_models,
            "model_identities": identities}


# -- per-case review verification -----------------------------------------
#
# Run 31330824658 created both PRs and exported an empty database: the driver
# never waited for a review and never called its own assertion functions. A
# fixture-creation driver that reaches cleanup is indistinguishable from a
# passing E2E, which is exactly the failure this section exists to prevent.

#: Bounded so a stalled runner fails with diagnostics instead of hanging.
#: Where the runner looks for the manifest. It fetches this file from the
#: repository at each SHA rather than parsing dbt itself, so both fixture
#: branches must carry one or no comparison is possible.
MANIFEST_PATH = "target/manifest.json"

DELIVERY_TIMEOUT = 240
REVIEW_TIMEOUT = 240
ATTEMPT_TIMEOUT = 240


def verify_case_delivery(since_utc: str, pr_number: int, head_sha: str) -> dict:
    """Prove GitHub delivered *this case's* pull_request event, accepted 202.

    `vf.verify_genuine_webhook` proves a delivery was accepted but does not
    bind it to a pull request. With two PRs in one run that is not enough --
    the BLOCK case could be satisfied by the ALLOW delivery. So the accepted
    delivery is then re-read and its payload matched to the exact PR number
    and head SHA.
    """
    delivery = vf.verify_genuine_webhook(GH, APP_JWT, since_utc, pr_number)
    # The helper returns a summary, not the raw delivery: `delivery_id` is the
    # GUID and its `pull_request` field is the argument echoed back, not
    # anything read from the payload. The detail endpoint is keyed by the
    # numeric id, so resolve that from the GUID before correlating.
    guid = delivery.get("delivery_id") if isinstance(delivery, dict) else None
    if not guid:
        raise StageFailure(
            f"accepted delivery for PR #{pr_number} carried no guid to correlate")
    list_status, deliveries = GH(
        "GET", "/app/hook/deliveries?per_page=50", APP_JWT())
    if list_status != 200 or not isinstance(deliveries, list):
        raise StageFailure(
            f"cannot list deliveries to correlate PR #{pr_number}: HTTP {list_status}")
    numeric = [item.get("id") for item in deliveries
               if isinstance(item, dict) and item.get("guid") == guid]
    if len(numeric) != 1 or numeric[0] is None:
        raise StageFailure(
            f"delivery guid {guid} matched {len(numeric)} numeric ids; "
            f"cannot correlate PR #{pr_number}")
    delivery_id = numeric[0]
    status, detail = GH("GET", f"/app/hook/deliveries/{delivery_id}", APP_JWT())
    if status != 200 or not isinstance(detail, dict):
        raise StageFailure(
            f"cannot read delivery {delivery_id} for PR #{pr_number}: HTTP {status}")
    payload = ((detail.get("request") or {}).get("payload")
               if isinstance(detail.get("request"), dict) else None) or {}
    pull = payload.get("pull_request") or {}
    delivered_number = pull.get("number")
    delivered_head = ((pull.get("head") or {}).get("sha")
                      if isinstance(pull.get("head"), dict) else None)
    if delivered_number != pr_number or delivered_head != head_sha:
        raise StageFailure(
            f"delivery {delivery_id} is for PR #{delivered_number} at "
            f"{str(delivered_head)[:12]}, not PR #{pr_number} at {head_sha[:12]}")
    return {"delivery_id": delivery_id, "delivery_guid": guid,
            "pull_number": delivered_number, "head_sha": delivered_head,
            "status_code": delivery.get("status_code"),
            "event": delivery.get("event"),
            "application_disposition": delivery.get("application_disposition"),
            "correlated": True}


def _review_diagnostics(dsn: str, pr_number: int, base_sha: str,
                        head_sha: str, delivery: dict) -> dict:
    """Everything needed to tell "never delivered" from "never reviewed"."""
    rows = []
    try:
        store = vf._store(dsn)
        try:
            found = store.connection.execute(
                "SELECT review_id, attempt, pull_number, base_sha, head_sha, "
                "lifecycle_state, decision FROM reviews "
                "WHERE organization_id=%s AND repository_id=%s",
                (OWNER, REPO_NAME)).fetchall()
            rows = [dict(row) for row in found]
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001
        rows = [{"error": type(exc).__name__}]
    return {"pull_number": pr_number, "base_sha": base_sha, "head_sha": head_sha,
            "delivery_observed": delivery,
            "reviews_for_repository": rows,
            "lifecycle_states_observed": sorted(
                {str(row.get("lifecycle_state")) for row in rows
                 if isinstance(row, dict)})}


#: The lifecycle state a review legitimately reaches on the semantic path.
#: A final ALLOW/WARN/BLOCK requires the collector, a metadata snapshot and
#: worker recomputation -- all of which the metadata-review E2E already
#: proves. Waiting for a decision here would make this driver fail on a
#: stage it does not own, and is what conflated the two E2Es.
SEMANTIC_TERMINAL_STATE = "WAITING_FOR_METADATA"


def wait_for_semantic_attempt(dsn: str, review_id: str, attempt: int) -> dict:
    """Wait until the exact attempt is metadata-waiting with durable evidence.

    `decision` and `health` are deliberately not wait conditions. A NULL
    decision while WAITING_FOR_METADATA is correct product behaviour, and
    treating it as failure would have this driver assert a verdict the
    product has not yet made -- and could not make without the collector.

    `semantic_evidence` NULL is a wait condition, because that column is the
    entire point of this E2E: SQL NULL means the comparison did not run, and
    an absent row must never be read as a clean comparison.
    """
    def ready():
        store = vf._store(dsn)
        try:
            rows = store.connection.execute(
                "SELECT * FROM review_attempts WHERE organization_id=%s AND "
                "repository_id=%s AND review_id=%s AND attempt=%s",
                (OWNER, REPO_NAME, review_id, attempt)).fetchall()
            if not rows:
                return None
            row = dict(rows[0])
            if row.get("lifecycle_state") != SEMANTIC_TERMINAL_STATE:
                return None
            if row.get("semantic_evidence") is None:
                return None
            return row
        finally:
            store.close()

    return lf.poll(ready, timeout=ATTEMPT_TIMEOUT, interval=4,
                   description=(f"review_attempts {review_id} attempt {attempt} "
                                f"in {SEMANTIC_TERMINAL_STATE} with semantic "
                                f"evidence"))


# -- what the remote run is responsible for proving ------------------------
#
# Semantic evidence only. `material_sql_changes` is an in-memory policy
# artifact that the lifecycle never persists -- proven by inspecting a real
# decided attempt -- so requiring it here would assert against a column that
# does not exist. The evidence-versus-policy behaviour it establishes is
# proven locally instead, in test_real_fixture_regressions, against the same
# real fixture SQL.

REQUIRED_REMOTE_EVIDENCE = {
    "block": ({"kind": "projection_expression_changed",
               "output_name": "net_order_amount"},
              {"kind": "join_removed", "relation": "int_order_refunds"}),
    "allow": ({"kind": "filter_changed"},),
}

_ACCEPTABLE_STATUS = ("evaluated", "partial")


def assert_remote_semantic_evidence(case: str, row: dict) -> dict:
    """Assert the persisted semantic evidence for one case. No verdict.

    Deliberately says nothing about decision or health: at this lifecycle
    stage the product has not decided, and inventing one would be the exact
    fabrication this E2E exists to rule out.
    """
    failures = []
    evidence = _decode(row.get("semantic_evidence"))
    if not isinstance(evidence, dict):
        failures.append("semantic_evidence is not a persisted object")
        evidence = {}
    status = evidence.get("status")
    if status not in _ACCEPTABLE_STATUS:
        failures.append(f"semantic evidence status is {status!r}, "
                        f"not one of {list(_ACCEPTABLE_STATUS)}")
    changes = semantic_changes(evidence)
    if not changes:
        failures.append("semantic evidence is empty; the fixture proved nothing")
    for required in REQUIRED_REMOTE_EVIDENCE.get(case, ()):
        if not any(_matches(change, required) for change in changes):
            failures.append(f"required semantic evidence missing: {required}")
    mutated = (sf.BLOCK_MUTATED_MODELS if case == "block"
               else sf.ALLOW_MUTATED_MODELS)
    unexplained = _unexplained(changes, mutated)
    if unexplained:
        failures.append(f"semantic changes not backed by the mutation: {unexplained}")
    state = row.get("lifecycle_state")
    if state != SEMANTIC_TERMINAL_STATE:
        failures.append(f"lifecycle state is {state!r}, not {SEMANTIC_TERMINAL_STATE}")
    return {"case": case, "passed": not failures, "failures": failures,
            "lifecycle_state": state, "semantic_evidence_status": status,
            "change_kinds": sorted({c.get("kind") for c in changes}),
            "change_count": len(changes),
            # Recorded verbatim, never asserted: at this stage a NULL
            # decision is the correct product state.
            "decision_at_this_stage": row.get("decision"),
            "health_at_this_stage": row.get("health")}


def _decode(value):
    """psycopg may return JSON text rather than a parsed object."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def verify_case(dsn: str, case: str, pr_number: int, base_sha: str,
                head_sha: str, since_utc: str) -> dict:
    """Prove one case end to end, from GitHub delivery to persisted verdict."""
    delivery = verify_case_delivery(since_utc, pr_number, head_sha)
    try:
        review = vf.verify_postgres_review(dsn, OWNER, REPO_NAME, head_sha, base_sha)
    except StageFailure as exc:
        diagnostics = _review_diagnostics(dsn, pr_number, base_sha, head_sha, delivery)
        _write(f"semantic-diff-{case}-review-timeout.json", diagnostics)
        raise StageFailure(
            f"{case}: no persisted review for PR #{pr_number} "
            f"head {head_sha[:12]}: {exc}. Diagnostics: "
            f"{json.dumps(diagnostics, default=str)[:1200]}") from exc
    try:
        row = wait_for_semantic_attempt(dsn, review["review_id"], review["attempt"])
    except StageFailure as exc:
        diagnostics = _review_diagnostics(dsn, pr_number, base_sha, head_sha, delivery)
        diagnostics["review"] = review
        _write(f"semantic-diff-{case}-attempt-timeout.json", diagnostics)
        raise StageFailure(
            f"{case}: review {review['review_id']} attempt {review['attempt']} "
            f"never reached {SEMANTIC_TERMINAL_STATE} with semantic evidence: "
            f"{exc}. Diagnostics: "
            f"{json.dumps(diagnostics, default=str)[:1200]}") from exc

    evidence = _decode(row.get("semantic_evidence")) or {}
    verdict = assert_remote_semantic_evidence(case, row)
    record = {
        "case": case, "pull_number": pr_number,
        "base_sha": base_sha, "head_sha": head_sha,
        "review_id": review["review_id"], "attempt": review["attempt"],
        "lifecycle_state": row.get("lifecycle_state"),
        "semantic_evidence_status": evidence.get("status"),
        "semantic_changes": semantic_changes(evidence),
        # Carried for the record, never asserted remotely: the collector and
        # worker that produce a verdict belong to the metadata-review E2E.
        "decision_at_this_stage": row.get("decision"),
        "health_at_this_stage": row.get("health"),
        "final_verdict": "NOT_DETERMINED_AT_THIS_STAGE",
        "genuine_delivery": delivery,
        "assertion": verdict,
    }
    if not verdict["passed"]:
        _write(f"semantic-diff-{case}-failed.json", record)
        raise StageFailure(
            f"{case} case assertions failed against persisted state: "
            f"{verdict['failures']}")
    _write(f"semantic-diff-{case}-verified.json", record)
    return record


def main() -> int:
    if CLEANUP_ONLY:
        result = cleanup("workflow-always-step")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("cleanup_passed") else 1

    _arm_cleanup()
    _initial_recovery()
    gate = _environment_gate()
    _write("semantic-diff-environment.json", gate)

    dsn = os.environ["RELIUM_DATABASE_URL"]
    storage = EV / f"storage-{RUN}"
    storage.mkdir(parents=True, exist_ok=True)
    lf.start_api(state, str(REPO_ROOT), dsn, storage,
                 os.environ["RELIUM_GITHUB_WEBHOOK_SECRET"],
                 os.environ["RELIUM_GITHUB_APP_ID"],
                 os.environ["RELIUM_GITHUB_PRIVATE_KEY_PATH"], EV / "api.log",
                 on_start=_persist_process)
    lf.start_tunnel(state, EV / "tunnel.log", on_start=_persist_process)

    preserved = preserve_webhook()
    record = _load_recovery()
    record["webhook_mutated"] = True  # durable BEFORE the mutating call
    _write_recovery(record)
    lf.point_webhook(state, GH, APP_JWT, state["tunnel"]["url"])
    webhook = lf.verify_webhook(GH, APP_JWT, state["tunnel"]["url"])

    default_branch, default_sha = _fixture_main_identity()
    main_files = dfp.read_fixture_project(
        GH, REPO, FIXTURE_TOKEN, default_sha,
        required_models=("fct_orders", "int_customer_orders"))

    # Each case is proven before the next begins. Creating both PRs and then
    # cleaning up is what let run 31330824658 export an empty database.
    verified = []
    for case in CASES:
        prepared = prepare_case(case, main_files)
        base_branch, head_branch = case_branches(RUN, case)
        make_branch(base_branch, default_sha)
        for path, content in _changed_files(main_files, prepared["base_files"]):
            commit_file(base_branch, path, content, f"relium semantic e2e {case} base")
        # The runner fetches the manifest FROM the repository at
        # config.manifest_path for both SHAs -- it does not parse dbt itself.
        # Run 31360420394 accepted the delivery and created no review because
        # this file was never committed, so there was nothing to compare.
        commit_file(base_branch, MANIFEST_PATH,
                    json.dumps(prepared["base_manifest"], indent=2, sort_keys=True),
                    f"relium semantic e2e {case}: parsed base manifest")
        base_head = _load_recovery()["owned_branch_heads"][base_branch]
        make_branch(head_branch, base_head)
        commit_file(head_branch, prepared["changed_path"],
                    prepared["head_files"][prepared["changed_path"]],
                    f"relium semantic e2e {case} head")
        commit_file(head_branch, MANIFEST_PATH,
                    json.dumps(prepared["head_manifest"], indent=2, sort_keys=True),
                    f"relium semantic e2e {case}: parsed head manifest")
        # Captured before the PR so the delivery search cannot match an
        # earlier case's event; correlation to this exact PR follows.
        since = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        head_sha = _load_recovery()["owned_branch_heads"][head_branch]
        number = open_pull(
            case, head_branch, base_branch,
            f"Relium semantic {case} fixture {RUN}",
            "Automated Relium semantic Before/After E2E fixture. Do not merge.")
        record = verify_case(dsn, case, number, base_head, head_sha, since)
        record["model_identities"] = prepared["model_identities"]
        verified.append(record)

    # Written only now: a case counts as completed once its assertions have
    # passed against persisted PostgreSQL state, never because a PR exists.
    result = {"preserved": preserved, "webhook": webhook, "cases": verified,
              "browser": "NOT_RUN"}
    _write("semantic-diff-run.json", result)
    print(json.dumps({case["case"]: {
        "review_id": case["review_id"], "attempt": case["attempt"],
        "lifecycle_state": case["lifecycle_state"],
        "semantic_evidence_status": case["semantic_evidence_status"],
        "semantic_changes": len(case["semantic_changes"]),
        "final_verdict": case["final_verdict"]} for case in verified},
        indent=2, sort_keys=True))

    outcome = cleanup("normal")
    if not outcome.get("cleanup_passed"):
        # Never a silent exit code: run 31330824658 failed this way and the
        # log showed nothing but "exit code 1".
        raise StageFailure(f"cleanup failed: {outcome.get('failures')}")
    return 0


def _changed_files(main_files: dict[str, str],
                   base_files: dict[str, str]) -> list[tuple[str, str]]:
    return [(path, content) for path, content in sorted(base_files.items())
            if main_files.get(path) != content]


if __name__ == "__main__":
    raise SystemExit(main())
