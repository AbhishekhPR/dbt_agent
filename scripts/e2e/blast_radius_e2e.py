"""Dedicated live proof for direct-only dbt blast-radius planning.

This driver is intentionally smaller than the metadata-review and governance
flows.  It proves one chain and nothing else::

    real dbt parse -> genuine unmerged fixture PR -> served webhook runner
      -> PostgreSQL review plan -> authenticated public review API

The promoted frontend is not present in the GitHub repository that runs this
workflow.  Consequently this driver never fabricates a frontend checkout or a
browser result; the artifact records that independent leg as NOT_RUN.

The fixture token is used only for the two owned fixture refs, their files, and
the unmerged pull request.  App authentication owns webhook operations.  No
warehouse or collector is started.
"""
from __future__ import annotations

import atexit
import base64
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import live_flow as lf  # noqa: E402
import verify_flow as vf  # noqa: E402
from live_flow import StageFailure, local  # noqa: E402
from metadata_review_e2e import app_jwt, gh, issue_token  # noqa: E402


REPO = os.environ.get("RELIUM_E2E_REPOSITORY", "AbhishekhPR/relium-e2e-dbt")
APP_SLUG = os.environ.get("RELIUM_E2E_APP_SLUG", "relium-e2e")
OWNER, REPO_NAME = REPO.split("/", 1)
FIXTURE_TOKEN = os.environ.get("RELIUM_E2E_FIXTURE_TOKEN", "")
RUN = os.environ.get("RELIUM_E2E_RUN_ID", uuid.uuid4().hex[:10])
EV = Path(sys.argv[1] if len(sys.argv) > 1 else "blast-radius-evidence")
CLEANUP_ONLY = "--cleanup-only" in sys.argv
EV.mkdir(parents=True, exist_ok=True)
RUN_RECOVERY = EV / "blast-radius-recovery.json"

# Importing the shared auth helper constructs the metadata driver's tracker.
# That tracker describes no run and must never be uploaded as blast proof.
_STRAY_TRACKER = EV / "stage-tracker.json"
if _STRAY_TRACKER.exists():
    _STRAY_TRACKER.unlink()

state = {"procs": [], "tunnel": None, "cleanup_done": False,
         "cleanup_result": None, "expected_slug": APP_SLUG,
         "mutated": False}


def _write(name: str, document) -> None:
    (EV / name).write_text(
        json.dumps(document, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _write_recovery(record) -> None:
    payload = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = RUN_RECOVERY.with_name(
        f".{RUN_RECOVERY.name}.{uuid.uuid4().hex}.tmp")
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
            # Directory handles are not fsync-able on every supported local OS.
            # The file itself and atomic replace are still mandatory.
            pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _model_path(files: dict[str, str], model_name: str) -> str:
    matches = sorted(
        path for path in files
        if path.startswith("models/") and path.endswith(f"/{model_name}.sql")
    )
    if len(matches) != 1:
        raise StageFailure(
            f"fixture main has {len(matches)} source files for model {model_name}")
    return matches[0]


def _fixture_files(main_files: dict[str, str], changed_fact: bool,
                   exposure_models: list[str]) -> dict[str, str]:
    """Overlay the proof on the real fixture-main project.

    Base adds only the real dbt exposure.  Head changes only the existing fact
    source file.  Existing staging/direct/transitive models are never replaced
    by harness-owned facsimiles.
    """
    if (len(exposure_models) != 2
            or any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
                   for name in exposure_models)):
        raise StageFailure("exposure requires exactly two valid manifest model names")
    exposure_dependencies = "".join(
        f"      - ref('{name}')\n" for name in exposure_models)
    files = dict(main_files)
    files["relium.yml"] = "enabled: true\nenforcement_mode: enforce\n"
    files["models/blast_radius_exposure.yml"] = (
        "version: 2\nexposures:\n  - name: revenue_dashboard\n"
        "    label: Revenue dashboard\n    type: dashboard\n"
        "    maturity: high\n    url: https://example.invalid/revenue\n"
        "    depends_on:\n" + exposure_dependencies +
        "    owner:\n      name: Relium E2E\n"
        "      email: e2e@example.invalid\n"
    )
    if changed_fact:
        fact_path = _model_path(files, "fct_orders")
        files[fact_path] = (
            files[fact_path].rstrip() +
            "\n\n-- Relium blast-radius E2E fact-only change; never merge.\n"
        )
    return files


def _is_dbt_project_text(path: str) -> bool:
    if path in {"dbt_project.yml", "profiles.yml", "packages.yml",
                "dependencies.yml"}:
        return True
    return (path.startswith(("models/", "macros/"))
            and path.endswith((".sql", ".yml", ".yaml")))


def _read_fixture_project(token: str, commit_sha: str) -> dict[str, str]:
    """Read the exact parse-relevant project tree from fixture main."""
    status, tree = gh(
        "GET", f"/repos/{REPO}/git/trees/{commit_sha}?recursive=1",
        token, bearer=False)
    if status != 200 or not isinstance(tree, dict):
        raise StageFailure(f"cannot read fixture-main Git tree: HTTP {status}")
    if tree.get("truncated"):
        raise StageFailure("fixture-main Git tree was truncated; parse would be incomplete")
    entries = [entry for entry in (tree.get("tree") or [])
               if entry.get("type") == "blob"
               and _is_dbt_project_text(entry.get("path") or "")]
    files = {}
    for entry in entries:
        blob_status, blob = gh(
            "GET", f"/repos/{REPO}/git/blobs/{entry['sha']}", token,
            bearer=False)
        if (blob_status != 200 or not isinstance(blob, dict)
                or blob.get("encoding") != "base64"):
            raise StageFailure(
                f"cannot read fixture-main blob {entry['path']}: HTTP {blob_status}")
        try:
            raw = base64.b64decode(blob.get("content") or "", validate=False)
            files[entry["path"]] = raw.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise StageFailure(
                f"fixture-main project file {entry['path']} is not UTF-8 text") from exc
    required = {"dbt_project.yml", "profiles.yml"}
    if not required.issubset(files):
        raise StageFailure(
            f"fixture-main project is missing {sorted(required - set(files))}")
    _model_path(files, "fct_orders")
    return files


def _parse_manifest(files: dict[str, str]) -> dict:
    dbt = shutil.which("dbt", path=str(Path(sys.executable).parent)) or shutil.which("dbt")
    if not dbt:
        raise StageFailure("dbt executable is unavailable; parse cannot be skipped")
    with tempfile.TemporaryDirectory(prefix="relium-blast-radius-") as temp:
        project = Path(temp)
        for relative, content in files.items():
            path = project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        # The dedicated fixture profile uses this relative SQLite directory.
        (project / "e2e" / "runtime").mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [dbt, "parse", "--project-dir", str(project),
             "--profiles-dir", str(project), "--no-partial-parse",
             "--no-version-check"],
            capture_output=True, text=True, check=False, timeout=90,
        )
        if completed.returncode != 0:
            detail = (completed.stdout + completed.stderr)[-2000:]
            raise StageFailure(f"dbt parse failed with rc={completed.returncode}: {detail}")
        manifest_path = project / "target" / "manifest.json"
        if not manifest_path.is_file():
            raise StageFailure("dbt parse returned success without target/manifest.json")
        return json.loads(manifest_path.read_text(encoding="utf-8"))


def _manifest_direct_models(manifest: dict, *, changed_model_name: str) -> dict:
    nodes = manifest.get("nodes") or {}
    changed = sorted(
        node_id for node_id, node in nodes.items()
        if node.get("resource_type") == "model" and node.get("name") == changed_model_name
    )
    if len(changed) != 1:
        raise StageFailure(
            f"manifest has {len(changed)} model identities named {changed_model_name}")
    changed_id = changed[0]
    direct_ids = sorted(
        node_id for node_id, node in nodes.items()
        if node.get("resource_type") == "model"
        and changed_id in ((node.get("depends_on") or {}).get("nodes") or [])
    )
    if len(direct_ids) != 2:
        raise StageFailure(
            f"manifest has {len(direct_ids)} direct model consumers, expected 2")
    direct_names = [nodes[node_id].get("name") for node_id in direct_ids]
    if any(not isinstance(name, str) or not name for name in direct_names):
        raise StageFailure("manifest direct model identity has no name")
    return {"changed_model_id": changed_id,
            "direct_model_ids": direct_ids,
            "direct_model_names": direct_names}


def _manifest_topology(manifest: dict, *, changed_model_name: str) -> dict:
    nodes = manifest.get("nodes") or {}
    sources = manifest.get("sources") or {}
    exposures = manifest.get("exposures") or {}
    direct_models = _manifest_direct_models(
        manifest, changed_model_name=changed_model_name)
    changed_id = direct_models["changed_model_id"]
    changed_deps = list((nodes[changed_id].get("depends_on") or {}).get("nodes") or [])
    staging = sorted(
        node_id for node_id in changed_deps
        if node_id in nodes and nodes[node_id].get("name") == "stg_orders"
    )
    if len(staging) != 1:
        raise StageFailure("changed fact is not downstream of the real stg_orders model")
    staging_id = staging[0]
    staging_deps = list((nodes[staging_id].get("depends_on") or {}).get("nodes") or [])
    order_sources = sorted(
        source_id for source_id in staging_deps
        if source_id in sources and sources[source_id].get("name") == "orders"
    )
    if len(order_sources) != 1:
        raise StageFailure("stg_orders is not downstream of the real orders source")
    source_id = order_sources[0]

    direct = direct_models["direct_model_ids"]
    transitive = sorted(
        node_id for node_id, node in nodes.items()
        if node.get("resource_type") == "model"
        and any(parent in direct for parent in
                ((node.get("depends_on") or {}).get("nodes") or []))
    )
    exposure_ids = sorted(
        exposure_id for exposure_id, exposure in exposures.items()
        if any(parent in direct for parent in
               ((exposure.get("depends_on") or {}).get("nodes") or []))
    )
    if not transitive:
        raise StageFailure("manifest lacks a transitive model exclusion probe")
    if not exposure_ids:
        raise StageFailure("manifest lacks a real exposure exclusion probe")
    return {
        "source_id": source_id,
        "staging_model_id": staging_id,
        "changed_model_id": changed_id,
        "direct_model_ids": direct,
        "transitive_model_id": transitive[0],
        "exposure_id": exposure_ids[0],
        "source_to_staging": source_id in staging_deps,
        "staging_to_changed_model": staging_id in changed_deps,
        "changed_to_direct_models": direct,
    }


def _verify_backend_truth(topology: dict, persisted_review: dict,
                          public_review: dict) -> dict:
    expected = topology["direct_model_ids"]
    if len(expected) != 2:
        raise StageFailure("manifest-derived direct model set is not exactly two")
    plan = ((persisted_review.get("payload") or {}).get("plan") or {})
    persisted = plan.get("downstream_models")
    if persisted != expected:
        raise StageFailure(
            f"persisted plan downstream models do not exactly match manifest: {persisted}")
    public = (public_review.get("change_plan") or {}).get("downstream_models")
    if public != expected:
        raise StageFailure(
            f"public API downstream models do not exactly match persisted plan: {public}")
    excluded = [topology["transitive_model_id"], topology["exposure_id"]]
    if any(node_id in persisted or node_id in public for node_id in excluded):
        raise StageFailure("transitive model or exposure leaked into direct-only result")
    return {
        "manifest_derived_direct_model_ids": expected,
        "persisted_downstream_model_ids": persisted,
        "public_api_downstream_model_ids": public,
        "excluded_transitive_model_id": topology["transitive_model_id"],
        "excluded_exposure_id": topology["exposure_id"],
        "exact_match": True,
    }


def _sanitized_public_review(review: dict) -> dict:
    allowed = (
        "review_id", "environment", "pull_number", "commit_sha", "decision",
        "lifecycle_state", "attempt", "enforcement_mode", "evidence_coverage",
        "metadata_required", "base_sha", "head_sha", "base_manifest_hash",
        "head_manifest_hash", "change_plan",
    )
    return {field: review.get(field) for field in allowed}


def _initial_recovery() -> dict:
    base_branch = f"e2e/blast-radius-base-{RUN}"
    head_branch = f"e2e/blast-radius-head-{RUN}"
    record = {
        "run_id": RUN,
        "repository": REPO,
        "expected_app_slug": APP_SLUG,
        "webhook_preserved": False,
        "webhook_mutated": False,
        "original_webhook": None,
        "pr_number": None,
        "branch_candidates": [base_branch, head_branch],
        "owned_branches": [],
        "branch_mutation_intents": [],
        "processes": [],
    }
    _write_recovery(record)
    # Keep the shared live_flow mutation guard sourced from the same exact
    # dedicated App identity that was durably recorded for this run.
    state["expected_slug"] = record["expected_app_slug"]
    state["mutated"] = False
    return record


def _load_recovery() -> dict | None:
    if not RUN_RECOVERY.is_file():
        return None
    try:
        record = json.loads(RUN_RECOVERY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StageFailure(f"cannot read blast-radius recovery record: {type(exc).__name__}")
    return record if isinstance(record, dict) else None


def _validate_branch_record(record: dict) -> tuple[list[str], list[str]]:
    run_id = record.get("run_id")
    if (not isinstance(run_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{6,32}", run_id) is None):
        raise StageFailure("owned branch record has an invalid run id")
    expected = [f"e2e/blast-radius-base-{run_id}",
                f"e2e/blast-radius-head-{run_id}"]
    if record.get("branch_candidates") != expected:
        raise StageFailure("branch candidate record must contain the exact base and head refs")
    owned = record.get("owned_branches")
    if not isinstance(owned, list) or owned not in ([], expected[:1], expected):
        raise StageFailure(
            "owned branch record must be an ordered proven subset of the exact refs")
    intents = record.get("branch_mutation_intents")
    if not isinstance(intents, list) or len(intents) > 1:
        raise StageFailure("branch mutation intent record must contain at most one intent")
    if intents:
        intent = intents[0]
        next_branch = expected[len(owned)] if len(owned) < len(expected) else None
        if (not isinstance(intent, dict)
                or intent.get("branch") != next_branch
                or intent.get("ref") != f"refs/heads/{next_branch}"
                or not isinstance(intent.get("expected_sha"), str)
                or not intent["expected_sha"]):
            raise StageFailure("branch mutation intent is not the exact next candidate")
    return expected, owned


def _validate_owned_branches(record: dict) -> list[str]:
    return _validate_branch_record(record)[1]


def preserve_webhook() -> dict:
    record = _load_recovery()
    if record is None:
        raise StageFailure("recovery record must exist before webhook preservation")
    status, hook = gh("GET", "/app/hook/config", app_jwt())
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
    jwt = app_jwt()
    patch_status, _ = gh(
        "PATCH", "/app/hook/config", jwt,
        {"url": original.get("url"),
         "content_type": original.get("content_type"),
         "insecure_ssl": original.get("insecure_ssl")},
    )
    get_status, confirmed = gh("GET", "/app/hook/config", jwt)
    url_matches = get_status == 200 and confirmed.get("url") == original.get("url")
    type_matches = (get_status == 200 and confirmed.get("content_type") ==
                    original.get("content_type"))
    ssl_matches = (get_status == 200 and confirmed.get("insecure_ssl") ==
                   original.get("insecure_ssl"))
    verified = patch_status == 200 and url_matches and type_matches and ssl_matches
    return {"restored": patch_status == 200,
            "patch_status": patch_status, "get_status": get_status,
            "url_matches_original": url_matches,
            "content_type_matches_original": type_matches,
            "insecure_ssl_matches_original": ssl_matches,
            "verified_through_github": verified, "secret_touched": False}


def _persist_process(label: str, proc, marker: str) -> None:
    record = _load_recovery()
    if record is None:
        raise StageFailure("cannot persist a process without a recovery record")
    if label not in {"api", "tunnel"} or marker not in {"uvicorn", "cloudflared"}:
        raise StageFailure("unexpected process identity")
    item = {"label": label, "pid": proc.pid, "marker": marker}
    processes = record.get("processes") or []
    if any(existing.get("pid") == proc.pid and existing != item
           for existing in processes):
        raise StageFailure("process PID is already recorded with another identity")
    if item not in processes:
        processes.append(item)
    record["processes"] = processes
    _write_recovery(record)


def _put_file(token: str, branch: str, path: str, content: str, message: str) -> str:
    body = {"message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch}
    status, existing = gh(
        "GET", f"/repos/{REPO}/contents/{path}?ref={branch}", token, bearer=False)
    if status == 200 and isinstance(existing, dict) and existing.get("sha"):
        body["sha"] = existing["sha"]
    elif status != 404:
        raise StageFailure(f"cannot inspect {path} on {branch}: HTTP {status}")
    put_status, response = gh(
        "PUT", f"/repos/{REPO}/contents/{path}", token, body, bearer=False)
    if put_status not in (200, 201):
        raise StageFailure(f"cannot commit {path} on {branch}: HTTP {put_status}")
    return response["commit"]["sha"]


def _make_branch(token: str, branch: str, from_sha: str) -> None:
    record = _load_recovery()
    if record is None:
        raise StageFailure("branch creation has no recovery record")
    candidates, owned = _validate_branch_record(record)
    if len(owned) >= len(candidates) or branch != candidates[len(owned)]:
        raise StageFailure("branch creation is not the next exact owned candidate")
    ref_path = f"/repos/{REPO}/git/ref/heads/{branch}"
    preflight_status, _ = gh("GET", ref_path, token, bearer=False)
    if preflight_status != 404:
        raise StageFailure(
            f"owned branch candidate {branch} was not absent at preflight")
    intent = {"branch": branch, "ref": f"refs/heads/{branch}",
              "expected_sha": from_sha}
    record = _load_recovery()
    candidates, owned = _validate_branch_record(record)
    if branch != candidates[len(owned)]:
        raise StageFailure("branch ownership changed before mutation intent")
    record["branch_mutation_intents"] = [intent]
    _write_recovery(record)  # durable BEFORE the mutating GitHub call
    response_error = None
    try:
        status, _ = gh(
            "POST", f"/repos/{REPO}/git/refs", token,
            {"ref": f"refs/heads/{branch}", "sha": from_sha}, bearer=False)
        if status != 201:
            raise StageFailure(f"cannot create owned branch {branch}: HTTP {status}")
    except (OSError, TimeoutError) as exc:
        response_error = exc
    verify_status, verified = gh("GET", ref_path, token, bearer=False)
    exact = (verify_status == 200
             and verified.get("ref") == f"refs/heads/{branch}"
             and (verified.get("object") or {}).get("type") == "commit"
             and (verified.get("object") or {}).get("sha") == from_sha)
    if not exact:
        detail = f" after {type(response_error).__name__}" if response_error else ""
        raise StageFailure(
            f"owned branch {branch} creation was not exactly verified{detail}")
    record = _load_recovery()
    candidates, owned = _validate_branch_record(record)
    if (branch != candidates[len(owned)]
            or record.get("branch_mutation_intents") != [intent]):
        raise StageFailure("owned branch intent changed during creation")
    record["owned_branches"] = owned + [branch]
    record["branch_mutation_intents"] = []
    try:
        _write_recovery(record)
    except Exception as exc:  # noqa: BLE001
        # A verified ref without a durable ownership record would be an
        # orphan that later cleanup must refuse to delete. Compensate using
        # only this exact just-created ref, then prove it absent.
        delete_status, _ = gh(
            "DELETE", f"/repos/{REPO}/git/refs/heads/{branch}",
            token, bearer=False)
        absent_status, _ = gh("GET", ref_path, token, bearer=False)
        if delete_status not in (204, 404) or absent_status != 404:
            raise StageFailure(
                f"owned branch {branch} could neither be recorded nor removed") from exc
        raise


def _create_fixture_pr(default_branch: str, default_sha: str,
                       main_files: dict[str, str], base_files: dict[str, str],
                       base_manifest: dict, head_files: dict[str, str],
                       head_manifest: dict) -> dict:
    if not FIXTURE_TOKEN:
        raise StageFailure("fixture token is required for fixture-only writes")
    record = _load_recovery()
    if record is None:
        raise StageFailure("fixture creation has no recovery record")
    (base_branch, head_branch), _owned = _validate_branch_record(record)
    status, ref = gh(
        "GET", f"/repos/{REPO}/git/ref/heads/{default_branch}",
        FIXTURE_TOKEN, bearer=False)
    if status != 200:
        raise StageFailure(f"cannot read fixture main ref: HTTP {status}")
    if ref["object"]["sha"] != default_sha:
        raise StageFailure("fixture main moved between parse and branch creation")

    _make_branch(FIXTURE_TOKEN, base_branch, default_sha)
    base_changes = sorted(
        path for path in set(main_files) | set(base_files)
        if main_files.get(path) != base_files.get(path)
    )
    if base_changes != ["models/blast_radius_exposure.yml", "relium.yml"]:
        raise StageFailure(
            f"ephemeral base changes more than config/exposure: {base_changes}")
    base_tip = None
    for path in base_changes:
        base_tip = _put_file(FIXTURE_TOKEN, base_branch, path, base_files[path],
                             f"blast radius {RUN}: base {path}")
    base_tip = _put_file(
        FIXTURE_TOKEN, base_branch, "target/manifest.json",
        json.dumps(base_manifest, indent=2, sort_keys=True),
        f"blast radius {RUN}: parsed base manifest")

    _make_branch(FIXTURE_TOKEN, head_branch, base_tip)
    changed_paths = sorted(path for path in set(base_files) | set(head_files)
                           if base_files.get(path) != head_files.get(path))
    fact_path = _model_path(base_files, "fct_orders")
    if changed_paths != [fact_path]:
        raise StageFailure(f"head source change is not fact-only: {changed_paths}")
    _put_file(FIXTURE_TOKEN, head_branch, changed_paths[0],
              head_files[changed_paths[0]], f"blast radius {RUN}: change fct_orders")
    _put_file(
        FIXTURE_TOKEN, head_branch, "target/manifest.json",
        json.dumps(head_manifest, indent=2, sort_keys=True),
        f"blast radius {RUN}: parsed head manifest")

    status, pull = gh(
        "POST", f"/repos/{REPO}/pulls", FIXTURE_TOKEN,
        {"title": f"[E2E FIXTURE - DO NOT MERGE] blast radius {RUN}",
         "head": head_branch, "base": base_branch, "draft": True,
         "body": ("Dedicated direct-downstream proof. Never merge; the owned "
                  "PR and both ephemeral refs are deleted during cleanup.")},
        bearer=False)
    if status != 201:
        raise StageFailure(f"cannot open blast-radius fixture PR: HTTP {status}")
    pull = _validate_created_pr_response(pull, [base_branch, head_branch])
    number = pull["number"]
    record = _load_recovery()
    _validate_branch_record(record)
    record["pr_number"] = number
    _write_recovery(record)
    return {"pr_number": number, "base_branch": base_branch,
            "head_branch": head_branch, "base_sha": pull["base"]["sha"],
            "head_sha": pull["head"]["sha"], "merged": False,
            "changed_model_files": changed_paths}


def _validate_created_pr_response(pull: dict, branches: list[str]) -> dict:
    base_branch, head_branch = branches
    number = pull.get("number") if isinstance(pull, dict) else None
    head = pull.get("head") if isinstance(pull, dict) else None
    base = pull.get("base") if isinstance(pull, dict) else None
    if (not isinstance(number, int) or isinstance(number, bool)
            or pull.get("draft") is not True or pull.get("state") != "open"
            or not isinstance(head, dict) or not isinstance(base, dict)
            or not isinstance(head.get("sha"), str) or not head.get("sha")
            or not isinstance(base.get("sha"), str) or not base.get("sha")):
        raise StageFailure("created fixture PR response is not a typed draft OPEN PR")
    if head.get("ref") != head_branch or base.get("ref") != base_branch:
        raise StageFailure("created fixture PR identity does not match exact owned refs")
    return pull


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _process_candidates(record: dict, failures: list[str]) -> list[dict]:
    candidates = []
    by_pid = {}
    valid_identities = {("api", "uvicorn"), ("tunnel", "cloudflared")}
    for item in record.get("processes") or []:
        if not isinstance(item, dict):
            failures.append("invalid durable process record")
            continue
        candidate = {key: item.get(key) for key in ("label", "pid", "marker")}
        pid = candidate["pid"]
        if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1
                or (candidate["label"], candidate["marker"])
                not in valid_identities):
            failures.append("invalid durable process identity")
            continue
        if pid in by_pid and by_pid[pid] != candidate:
            failures.append("durable process PID has conflicting identities")
            continue
        if pid not in by_pid:
            by_pid[pid] = candidate
            candidates.append(candidate)
    in_memory = [
        {"label": label, "pid": proc.pid, "marker": "uvicorn"}
        for label, proc in state.get("procs", [])
    ]
    if state.get("tunnel") and state["tunnel"].get("proc"):
        in_memory.append({"label": "tunnel",
                          "pid": state["tunnel"]["proc"].pid,
                          "marker": "cloudflared"})
    for candidate in in_memory:
        pid = candidate["pid"]
        if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1
                or (candidate["label"], candidate["marker"])
                not in valid_identities):
            failures.append("invalid in-memory process identity")
            continue
        if pid in by_pid and by_pid[pid] != candidate:
            failures.append("in-memory process PID conflicts with durable identity")
        elif pid not in by_pid:
            by_pid[pid] = candidate
            candidates.append(candidate)
    return candidates


def _stop_recorded_processes(record: dict, failures: list[str]) -> list[dict]:
    results = []
    handles = {proc.pid: proc for _label, proc in state.get("procs", [])}
    if state.get("tunnel"):
        tunnel_proc = state["tunnel"]["proc"]
        handles[tunnel_proc.pid] = tunnel_proc
    for item in _process_candidates(record, failures):
        pid, marker = item.get("pid"), item.get("marker")
        outcome = {"label": item.get("label"), "pid": pid, "stopped": False}
        if not isinstance(pid, int) or pid <= 1 or not isinstance(marker, str):
            failures.append("invalid recorded process identity")
            results.append(outcome)
            continue
        if not _process_alive(pid):
            outcome["stopped"] = True
            outcome["already_absent"] = True
            results.append(outcome)
            continue
        proc_path = Path(f"/proc/{pid}/cmdline")
        command = proc_path.read_bytes().replace(b"\x00", b" ").decode(
            "utf-8", errors="replace") if proc_path.is_file() else ""
        if marker not in command:
            failures.append(f"recorded {item.get('label')} PID signature does not match")
            results.append(outcome)
            continue
        handle = handles.get(pid)
        try:
            if handle:
                handle.terminate()
                try:
                    handle.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    handle.kill()
                    handle.wait(timeout=5)
            else:
                os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + 10
                while _process_alive(pid) and time.monotonic() < deadline:
                    time.sleep(0.1)
                if _process_alive(pid):
                    os.kill(pid, signal.SIGKILL)
                    time.sleep(0.2)
            outcome["stopped"] = not _process_alive(pid)
            if not outcome["stopped"]:
                failures.append(f"recorded {item.get('label')} process survived cleanup")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"stopping {item.get('label')}: {type(exc).__name__}")
        results.append(outcome)
    return results


def _listener_up(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _validate_owned_pr(pull: dict, branches: list[str]) -> dict:
    base_branch, head_branch = branches
    if not isinstance(pull, dict):
        raise StageFailure("owned fixture PR is not a typed object")
    number = pull.get("number")
    state_value = pull.get("state")
    merged = pull.get("merged")
    head = pull.get("head")
    base = pull.get("base")
    typed = (isinstance(number, int) and not isinstance(number, bool)
             and state_value in {"open", "closed"}
             and isinstance(merged, bool)
             and isinstance(head, dict) and isinstance(base, dict)
             and head.get("ref") == head_branch
             and base.get("ref") == base_branch)
    if not typed:
        raise StageFailure("owned fixture PR identity/state is not exactly typed")
    return pull


def _discover_owned_pr(branches: list[str]) -> dict | None:
    """Recover an accepted PR response across every state, or prove absence."""
    base_branch, head_branch = branches
    query = urllib.parse.urlencode({
        "state": "all",
        "head": f"{OWNER}:{head_branch}",
        "base": base_branch,
        "per_page": 100,
    })
    status, pulls = gh(
        "GET", f"/repos/{REPO}/pulls?{query}", FIXTURE_TOKEN, bearer=False)
    if status != 200 or not isinstance(pulls, list):
        raise StageFailure(f"cannot discover exact owned fixture PR: HTTP {status}")
    if len(pulls) != 1:
        if not pulls:
            return None
        raise StageFailure(
            f"exact owned head/base pair has {len(pulls)} pull requests")
    summary = pulls[0]
    number = summary.get("number") if isinstance(summary, dict) else None
    if (not isinstance(number, int) or isinstance(number, bool)
            or summary.get("state") not in {"open", "closed"}
            or not isinstance(summary.get("head"), dict)
            or not isinstance(summary.get("base"), dict)
            or summary["head"].get("ref") != head_branch
            or summary["base"].get("ref") != base_branch):
        raise StageFailure("discovered fixture PR summary is not exactly typed")
    detail_status, detail = gh(
        "GET", f"/repos/{REPO}/pulls/{number}", FIXTURE_TOKEN, bearer=False)
    if detail_status != 200:
        raise StageFailure(
            f"cannot verify discovered fixture PR #{number}: HTTP {detail_status}")
    return _validate_owned_pr(detail, branches)


def _resolve_branch_mutation_intent(record: dict,
                                    failures: list[str]) -> tuple[dict, bool]:
    candidates, owned = _validate_branch_record(record)
    intents = record["branch_mutation_intents"]
    if not intents:
        return record, True
    if not FIXTURE_TOKEN:
        failures.append("fixture token unavailable for branch mutation intent")
        return record, False
    intent = intents[0]
    branch = intent["branch"]
    status, ref = gh(
        "GET", f"/repos/{REPO}/git/ref/heads/{branch}",
        FIXTURE_TOKEN, bearer=False)
    if status == 404:
        record["branch_mutation_intents"] = []
    elif status == 200:
        exact = (ref.get("ref") == intent["ref"]
                 and (ref.get("object") or {}).get("type") == "commit"
                 and (ref.get("object") or {}).get("sha") == intent["expected_sha"])
        if not exact:
            failures.append(
                f"branch mutation intent {branch} does not exactly match GitHub ref")
            return record, False
        if branch != candidates[len(owned)]:
            failures.append("branch mutation intent cannot preserve ownership order")
            return record, False
        record["owned_branches"] = owned + [branch]
        record["branch_mutation_intents"] = []
    else:
        failures.append(
            f"branch mutation intent {branch} exact GET returned HTTP {status}")
        return record, False
    try:
        _write_recovery(record)
    except Exception as exc:  # noqa: BLE001
        failures.append(
            f"branch mutation intent resolution was not durable: {type(exc).__name__}")
        return record, False
    return _load_recovery(), True


def cleanup(reason: str = "normal") -> dict:
    if state.get("cleanup_done"):
        return state.get("cleanup_result") or {}
    state["cleanup_done"] = True
    result = {"reason": reason, "failures": [], "nothing_owned": False,
              "fixture_pr": None, "fixture_branches_deleted": []}
    record = _load_recovery()
    if record is None:
        result.update({
            "nothing_owned": True,
            "webhook": {"restored": None, "verified_through_github": False,
                        "reason": "no recovery record; no mutation is attributed"},
            "processes": [],
            "listeners_remaining": [port for port in (lf.PORT, 5181)
                                    if _listener_up(port)],
        })
        if result["listeners_remaining"]:
            result["failures"].append(
                f"local E2E listeners remain: {result['listeners_remaining']}")
        result["cleanup_passed"] = not result["failures"]
        state["cleanup_result"] = result
        _write("blast-radius-cleanup.json", result)
        return result

    branches = None
    candidates = None
    try:
        candidates, branches = _validate_branch_record(record)
    except Exception as exc:  # noqa: BLE001
        result["failures"].append(str(exc))

    # The webhook is the only global external mutation and is always restored
    # before fixture refs or local processes are touched.
    if record.get("webhook_mutated"):
        try:
            result["webhook"] = restore_webhook(record)
            if not result["webhook"].get("verified_through_github"):
                result["failures"].append("webhook restoration not verified by exact GET")
        except Exception as exc:  # noqa: BLE001
            result["webhook"] = {"restored": False,
                                 "verified_through_github": False,
                                 "error": type(exc).__name__}
            result["failures"].append(
                f"webhook restoration raised {type(exc).__name__}")
    else:
        result["webhook"] = {"restored": None,
                             "verified_through_github": False,
                             "reason": "record attributes no webhook mutation"}

    intents_resolved = True
    if candidates is not None:
        try:
            record, intents_resolved = _resolve_branch_mutation_intent(
                record, result["failures"])
            candidates, branches = _validate_branch_record(record)
        except Exception as exc:  # noqa: BLE001
            intents_resolved = False
            result["failures"].append(
                f"branch mutation intent resolution raised {type(exc).__name__}")

    if branches and intents_resolved:
        if not FIXTURE_TOKEN:
            result["failures"].append("fixture token unavailable for owned cleanup")
        else:
            number = record.get("pr_number")
            pull = None
            refs_safe_to_delete = False
            if number is None:
                try:
                    pull = _discover_owned_pr(candidates)
                    refs_safe_to_delete = pull is None
                    number = pull.get("number") if pull else None
                except Exception as exc:  # noqa: BLE001
                    result["failures"].append(str(exc))
            if number is not None:
                try:
                    if pull is None:
                        status, pull = gh(
                            "GET", f"/repos/{REPO}/pulls/{number}",
                            FIXTURE_TOKEN, bearer=False)
                        if status != 200:
                            raise StageFailure(
                                f"cannot read owned fixture PR #{number}: HTTP {status}")
                        pull = _validate_owned_pr(pull, candidates)
                    if pull.get("merged"):
                        raise StageFailure(f"owned fixture PR #{number} was MERGED")
                    if pull.get("state") != "closed":
                        close_status, _ = gh(
                            "PATCH", f"/repos/{REPO}/pulls/{number}", FIXTURE_TOKEN,
                            {"state": "closed"}, bearer=False)
                        if close_status != 200:
                            result["failures"].append(
                                f"cannot close owned fixture PR #{number}: HTTP {close_status}")
                    verify_status, verified_pull = gh(
                        "GET", f"/repos/{REPO}/pulls/{number}",
                        FIXTURE_TOKEN, bearer=False)
                    if verify_status != 200:
                        raise StageFailure(
                            f"owned fixture PR #{number} close was not readable")
                    verified_pull = _validate_owned_pr(verified_pull, candidates)
                    pr_ok = (verified_pull["state"] == "closed"
                             and not verified_pull["merged"])
                    result["fixture_pr"] = {
                        "number": number,
                        "state": verified_pull.get("state"),
                        "merged": verified_pull.get("merged"),
                        "verified_through_github": pr_ok,
                    }
                    if not pr_ok:
                        raise StageFailure(
                            f"owned fixture PR #{number} close was not verified")
                    refs_safe_to_delete = True
                except Exception as exc:  # noqa: BLE001
                    result["failures"].append(str(exc))

            for branch in branches if refs_safe_to_delete else []:
                delete_status, _ = gh(
                    "DELETE", f"/repos/{REPO}/git/refs/heads/{branch}",
                    FIXTURE_TOKEN, bearer=False)
                if delete_status not in (204, 404):
                    result["failures"].append(
                        f"owned branch {branch} delete returned HTTP {delete_status}")
                    continue
                verify_status, _ = gh(
                    "GET", f"/repos/{REPO}/git/ref/heads/{branch}",
                    FIXTURE_TOKEN, bearer=False)
                if verify_status != 404:
                    result["failures"].append(
                        f"owned branch {branch} absence was not verified")
                else:
                    result["fixture_branches_deleted"].append(branch)

    result["processes"] = _stop_recorded_processes(record, result["failures"])
    result["listeners_remaining"] = [port for port in (lf.PORT, 5181)
                                     if _listener_up(port)]
    if result["listeners_remaining"]:
        result["failures"].append(
            f"local E2E listeners remain: {result['listeners_remaining']}")
    result["cleanup_passed"] = not result["failures"]
    state["cleanup_result"] = result
    _write("blast-radius-cleanup.json", result)
    print(f"[blast-cleanup:{reason}] passed={result['cleanup_passed']} "
          f"failures={result['failures']}", flush=True)
    return result


def _arm_cleanup() -> None:
    atexit.register(cleanup, "atexit")
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda value, _frame: (
                cleanup(f"signal-{value}"), sys.exit(130)))
        except (ValueError, OSError):
            pass


def _environment_gate() -> dict:
    if not FIXTURE_TOKEN:
        raise StageFailure("fixture token is not configured")
    jwt = app_jwt()
    status, app = gh("GET", "/app", jwt)
    slug = app.get("slug") if status == 200 else None
    if slug != APP_SLUG or "pilot" in str(slug).lower():
        raise StageFailure(f"unexpected GitHub App identity: {slug}")
    events = sorted(app.get("events") or [])
    if "pull_request" not in events:
        raise StageFailure("dedicated E2E App is not subscribed to pull_request")
    installation = __import__("metadata_review_e2e").installation_token(jwt)
    status, repositories = gh(
        "GET", "/installation/repositories", installation, bearer=False)
    names = sorted(item["full_name"] for item in
                   (repositories.get("repositories") or [])) if status == 200 else []
    if names != [REPO]:
        raise StageFailure(f"dedicated App repository scope is {names}")
    fixture_scope = lf.assert_fixture_token_scope(gh, FIXTURE_TOKEN, REPO)
    if fixture_scope.get("private_repositories_visible") != [REPO]:
        raise StageFailure("fixture token private repository scope is not exact")
    return {"app_slug": slug, "app_events": events, "app_repositories": names,
            "fixture_private_repositories":
                fixture_scope.get("private_repositories_visible")}


def _fixture_main_identity() -> tuple[str, str]:
    status, repo_info = gh("GET", f"/repos/{REPO}", FIXTURE_TOKEN, bearer=False)
    if status != 200:
        raise StageFailure(f"cannot read fixture repository: HTTP {status}")
    default_branch = repo_info.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise StageFailure("fixture repository has no default branch identity")
    status, ref = gh(
        "GET", f"/repos/{REPO}/git/ref/heads/{default_branch}",
        FIXTURE_TOKEN, bearer=False)
    if status != 200:
        raise StageFailure(f"cannot read fixture main ref: HTTP {status}")
    return default_branch, ref["object"]["sha"]


def main() -> int:
    if CLEANUP_ONLY:
        result = cleanup("workflow-always-step")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("cleanup_passed") else 1

    _arm_cleanup()
    record = _initial_recovery()
    gate = _environment_gate()

    dsn = os.environ["RELIUM_DATABASE_URL"]
    storage = EV / f"storage-{RUN}"
    storage.mkdir(parents=True, exist_ok=True)
    api = lf.start_api(
        state, str(REPO_ROOT), dsn, storage,
        os.environ["RELIUM_GITHUB_WEBHOOK_SECRET"],
        os.environ["RELIUM_GITHUB_APP_ID"],
        os.environ["RELIUM_GITHUB_PRIVATE_KEY_PATH"], EV / "api.log",
        on_start=_persist_process)
    tunnel = lf.start_tunnel(
        state, EV / "tunnel.log", on_start=_persist_process)

    preserved = preserve_webhook()
    record = _load_recovery()
    record["webhook_mutated"] = True  # durable BEFORE the mutating GitHub call
    _write_recovery(record)
    lf.point_webhook(state, gh, app_jwt, state["tunnel"]["url"])
    webhook = lf.verify_webhook(gh, app_jwt, state["tunnel"]["url"])

    default_branch, default_sha = _fixture_main_identity()
    main_files = _read_fixture_project(FIXTURE_TOKEN, default_sha)
    main_manifest = _parse_manifest(main_files)
    discovered = _manifest_direct_models(
        main_manifest, changed_model_name="fct_orders")
    base_files = _fixture_files(
        main_files, changed_fact=False,
        exposure_models=discovered["direct_model_names"])
    base_manifest = _parse_manifest(base_files)
    head_files = _fixture_files(
        main_files, changed_fact=True,
        exposure_models=discovered["direct_model_names"])
    head_manifest = _parse_manifest(head_files)
    topology = _manifest_topology(head_manifest, changed_model_name="fct_orders")

    since = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    pull = _create_fixture_pr(
        default_branch, default_sha, main_files, base_files, base_manifest,
        head_files, head_manifest)
    delivery = vf.verify_genuine_webhook(gh, app_jwt, since, pull["pr_number"])
    review = vf.verify_postgres_review(
        dsn, OWNER, REPO_NAME, pull["head_sha"], pull["base_sha"])

    from agent.postgres_lifecycle_store import PostgresLifecycleStore
    store = PostgresLifecycleStore(dsn)
    try:
        persisted = store.get_review(OWNER, REPO_NAME, review["review_id"])
    finally:
        store.close()
    token = issue_token(dsn, OWNER, REPO_NAME)
    status, public = local(
        "GET", f"/api/reviews/{review['review_id']}", token=token)
    if status != 200:
        raise StageFailure(f"authenticated public review API returned HTTP {status}")
    backend_truth = _verify_backend_truth(topology, persisted, public)

    evidence = {
        "run_id": RUN,
        "repository": REPO,
        "fixture_pull_request": pull,
        "environment_gate": gate,
        "api_started": api,
        "tunnel_started": {"healthy": bool(tunnel), "url_recorded": True},
        "webhook_preserved": preserved,
        "webhook_repoint_verified": webhook,
        "genuine_webhook": delivery,
        "manifest_topology": topology,
        "backend_truth": backend_truth,
        "public_review": _sanitized_public_review(public),
        "frontend_browser_proof": {
            "status": "NOT_RUN",
            "verified": False,
            "reason": ("promoted frontend source is unavailable in the GitHub "
                       "repository; no checkout, fake API, or browser result was invented"),
        },
        "warehouse_started": False,
        "collector_started": False,
    }
    _write("blast-radius-e2e.json", evidence)

    result = cleanup("normal")
    if not result.get("cleanup_passed"):
        raise StageFailure(f"cleanup failed: {result.get('failures')}")
    print(json.dumps({"review_id": review["review_id"],
                      "direct_model_ids": topology["direct_model_ids"],
                      "frontend_browser_proof": "NOT_RUN"}, indent=2))
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except Exception as exc:  # noqa: BLE001
        import traceback
        detail = traceback.format_exc()
        print(f"BLAST RADIUS E2E FAILED: {type(exc).__name__}: {exc}", flush=True)
        print(detail, flush=True)
        _write("blast-radius-failure.json", {
            "error": type(exc).__name__, "detail": str(exc)[:2000],
            "traceback": detail[-4000:],
            "frontend_browser_proof": {"status": "NOT_RUN", "verified": False},
        })
        code = 1
    finally:
        outcome = cleanup("finalizer")
        if not outcome.get("cleanup_passed", False):
            code = 1
    raise SystemExit(code)
