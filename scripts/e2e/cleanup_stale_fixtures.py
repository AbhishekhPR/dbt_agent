"""Close and remove stale ephemeral branches in the dedicated E2E repo.

This utility receives only the repository-scoped fixture credential.  It has
no GitHub App identity, private key, webhook secret, or webhook endpoint code.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.github.com"
REPOSITORY = "AbhishekhPR/relium-e2e-dbt"
EPHEMERAL_PREFIXES = ("e2e/", "phase7-")
REVIEWED_STALE_PULL_REQUESTS = {
    2: "phase7-20260802-safe-shadow",
    5: "phase7-20260802-safe-shadow-corrected",
    6: "phase7-20260802-l02",
    7: "phase7-20260802-l03",
    8: "phase7-20260802-l04",
    9: "phase7-20260802-l05",
    10: "phase7-20260802-l06",
    11: "phase7-20260802-l07",
    12: "phase7-20260802-l09",
    13: "phase7-20260802-l10",
    14: "phase7-20260802-l11",
    15: "phase7-20260802-l12",
    16: "phase7-20260802-l13",
    17: "phase7-20260802-l12-corrected",
}
REVIEWED_STALE_BRANCHES = frozenset({
    *REVIEWED_STALE_PULL_REQUESTS.values(),
    "phase7-20260802-l04-base",
})
EVIDENCE_DIR = Path(sys.argv[1])
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
TOKEN = os.environ.get("RELIUM_E2E_FIXTURE_TOKEN", "")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def gh(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(API + path, data=data, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "relium-fixture-cleanup")
    request.add_header("Authorization", f"token {TOKEN}")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return exc.code, {}


def _is_ephemeral(branch: str) -> bool:
    return branch.startswith(EPHEMERAL_PREFIXES)


def list_all(path: str, gh_fn=gh):
    values = []
    page = 1
    while True:
        separator = "&" if "?" in path else "?"
        status, batch = gh_fn(
            "GET", f"{path}{separator}per_page=100&page={page}")
        require(status == 200 and isinstance(batch, list),
                f"could not paginate {path}: HTTP {status}")
        values.extend(batch)
        if len(batch) < 100:
            return values
        page += 1


def _open_pulls():
    return list_all(f"/repos/{REPOSITORY}/pulls?state=open")


def _branches():
    branches = list_all(f"/repos/{REPOSITORY}/branches")
    return [branch["name"] for branch in branches]


def main() -> int:
    require(bool(TOKEN), "fixture cleanup credential is missing")
    status, repository = gh("GET", f"/repos/{REPOSITORY}")
    default_branch = repository.get("default_branch")
    require(status == 200 and repository.get("full_name") == REPOSITORY,
            "fixture credential cannot confirm the dedicated repository")
    require(repository.get("private") is True,
            "refusing cleanup outside the private dedicated fixture repository")
    require(default_branch == "main", "unexpected fixture default branch")

    pulls = _open_pulls()
    ephemeral_pulls = [pull for pull in pulls
                       if _is_ephemeral((pull.get("head") or {}).get("ref") or "")]
    unknown_pulls = []
    targets = []
    for pull in ephemeral_pulls:
        head = pull.get("head") or {}
        branch = head.get("ref") or ""
        head_repository = (head.get("repo") or {}).get("full_name")
        if (REVIEWED_STALE_PULL_REQUESTS.get(pull["number"]) != branch
                or head_repository != REPOSITORY):
            unknown_pulls.append({"number": pull["number"], "branch": branch})
        else:
            targets.append(pull)
    require(not unknown_pulls,
            f"unknown ephemeral pull requests require review: {unknown_pulls}")
    closed = []
    for pull in targets:
        number = pull["number"]
        status, _ = gh("PATCH", f"/repos/{REPOSITORY}/pulls/{number}",
                       {"state": "closed"})
        require(status == 200, f"could not close fixture PR #{number}: HTTP {status}")
        closed.append(number)

    branches = _branches()
    unknown_branches = [branch for branch in branches
                        if (_is_ephemeral(branch)
                            and branch not in REVIEWED_STALE_BRANCHES)]
    require(not unknown_branches,
            f"unknown ephemeral branches require review: {unknown_branches}")
    deleted = []
    for branch in branches:
        if branch not in REVIEWED_STALE_BRANCHES or branch == default_branch:
            continue
        encoded = urllib.parse.quote(branch, safe="")
        status, _ = gh("DELETE", f"/repos/{REPOSITORY}/git/refs/heads/{encoded}")
        require(status == 204,
                f"could not delete fixture branch {branch}: HTTP {status}")
        deleted.append(branch)

    remaining_pull_requests = [
        pull["number"] for pull in _open_pulls()
        if _is_ephemeral((pull.get("head") or {}).get("ref") or "")]
    remaining_branches = [branch for branch in _branches()
                          if _is_ephemeral(branch) and branch != default_branch]
    require(not remaining_pull_requests,
            f"ephemeral fixture PRs remain open: {remaining_pull_requests}")
    require(not remaining_branches,
            f"ephemeral fixture branches remain: {remaining_branches}")

    evidence = {
        "evidence_type": "stale-ephemeral-fixture-cleanup",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repository": REPOSITORY,
        "default_branch": default_branch,
        "ephemeral_prefixes": list(EPHEMERAL_PREFIXES),
        "pull_requests_closed": sorted(closed),
        "pull_requests_merged": [],
        "branches_deleted": sorted(deleted),
        "remaining_pull_requests": remaining_pull_requests,
        "remaining_branches": remaining_branches,
        "default_branch_preserved": default_branch in _branches(),
        "webhook_administration_performed": False,
        "app_credentials_available": False,
        "secret_exposed": False,
    }
    (EVIDENCE_DIR / "stale-fixture-cleanup.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"closed {len(closed)} PR(s); deleted {len(deleted)} branch(es)",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
