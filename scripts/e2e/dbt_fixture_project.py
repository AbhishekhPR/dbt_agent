"""Reading and parsing the fixture dbt project.

Extracted from blast_radius_e2e so the semantic driver shares one
implementation rather than a second copy that can drift. Everything here is
pure: it reads the fixture repository, writes into a temporary directory, and
runs dbt. It records no ownership, mutates nothing remote, and therefore
carries none of the safety-sensitive state that the two drivers keep
deliberately separate.

`parse_manifest` never falls back to a hand-authored manifest. If dbt cannot
parse the project the caller must fail: a fabricated manifest would make the
whole E2E assert its own opinion.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from live_flow import StageFailure


#: Files that change what `dbt parse` produces. Anything else in the fixture
#: repository (README, evidence, CI) is irrelevant to a manifest and is left
#: out so the parse input stays exactly the project.
def is_dbt_project_text(path: str) -> bool:
    if path in {"dbt_project.yml", "profiles.yml", "packages.yml",
                "dependencies.yml"}:
        return True
    return (path.startswith(("models/", "macros/"))
            and path.endswith((".sql", ".yml", ".yaml")))


def model_path(files: dict[str, str], model_name: str) -> str:
    """The single source file for a model, or a failure.

    Ambiguity is fatal rather than resolved by preference: two files named
    `<model>.sql` mean the harness cannot know which one it is mutating.
    """
    matches = sorted(
        path for path in files
        if path.startswith("models/") and path.endswith(f"/{model_name}.sql")
    )
    if len(matches) != 1:
        raise StageFailure(
            f"fixture main has {len(matches)} source files for model {model_name}")
    return matches[0]


def read_fixture_project(gh, repo: str, token: str, commit_sha: str,
                         *, required_models: tuple[str, ...] = ()) -> dict[str, str]:
    """Read the exact parse-relevant project tree at one commit."""
    status, tree = gh(
        "GET", f"/repos/{repo}/git/trees/{commit_sha}?recursive=1", token,
        bearer=False)
    if status != 200 or not isinstance(tree, dict):
        raise StageFailure(f"cannot read fixture Git tree: HTTP {status}")
    if tree.get("truncated"):
        raise StageFailure("fixture Git tree was truncated; parse would be incomplete")
    entries = [entry for entry in (tree.get("tree") or [])
               if entry.get("type") == "blob"
               and is_dbt_project_text(entry.get("path") or "")]
    files: dict[str, str] = {}
    for entry in entries:
        blob_status, blob = gh(
            "GET", f"/repos/{repo}/git/blobs/{entry['sha']}", token, bearer=False)
        if (blob_status != 200 or not isinstance(blob, dict)
                or blob.get("encoding") != "base64"):
            raise StageFailure(
                f"cannot read fixture blob {entry['path']}: HTTP {blob_status}")
        try:
            raw = base64.b64decode(blob.get("content") or "", validate=False)
            files[entry["path"]] = raw.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise StageFailure(
                f"fixture project file {entry['path']} is not UTF-8 text") from exc
    required = {"dbt_project.yml", "profiles.yml"}
    if not required.issubset(files):
        raise StageFailure(
            f"fixture project is missing {sorted(required - set(files))}")
    for name in required_models:
        model_path(files, name)
    return files


def parse_manifest(files: dict[str, str], *, prefix: str) -> dict:
    """Real `dbt parse` over a temporary copy of the project."""
    dbt = shutil.which("dbt", path=str(Path(sys.executable).parent)) or shutil.which("dbt")
    if not dbt:
        raise StageFailure("dbt executable is unavailable; parse cannot be skipped")
    with tempfile.TemporaryDirectory(prefix=prefix) as temp:
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


def model_identity(manifest: dict, model_name: str) -> str:
    """The single unique_id for a model name, or a failure.

    Model identity must be stable across base and head, or the comparison
    would be diffing two different nodes and calling it a change.
    """
    nodes = manifest.get("nodes") or {}
    matches = sorted(
        node_id for node_id, node in nodes.items()
        if node.get("resource_type") == "model" and node.get("name") == model_name
    )
    if len(matches) != 1:
        raise StageFailure(
            f"manifest has {len(matches)} model identities named {model_name}")
    return matches[0]
