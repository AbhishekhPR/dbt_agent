import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Mapping


MARKER = "<!-- relium-pr-guard -->"
GITHUB_API = "https://api.github.com"


def render_pr_comment(report: dict) -> str:
    safe = "YES" if report["safe_to_merge"] else "NO"
    lines = [
        MARKER,
        "",
        "## Relium PR Guard",
        "",
        f"Safe to merge: {safe}",
        "",
    ]

    if not report["risks"]:
        lines.extend(
            [
                "No risky SQL/dbt transformation logic was detected in the scanned files.",
                "",
            ]
        )
        return "\n".join(lines)

    lines.extend(
        [
            f"Project: {report['project']}",
            f"Files scanned: {report['files_scanned']}",
            f"Risks found: {report['risks_found']}",
            f"Highest severity: {report['highest_severity']}",
            "",
            f"### {_severity_title(report['highest_severity'])} risk transformation logic found",
            "",
        ]
    )

    for risk in report["risks"]:
        lines.extend(
            [
                f"#### {risk['model']}",
                "",
                "Risk:",
                _sentence(risk["message"]),
                "",
                "Evidence:",
                f"`{risk['evidence']}`",
                "",
                "Why it matters:",
                risk["why_it_matters"],
                "",
                "Suggested fix:",
                "",
                "```sql",
                risk["suggested_fix"],
                "```",
                "",
                "Affected downstream models:",
                "",
                *_asterisk_lines(risk["affected_downstream_models"]),
                "",
            ]
        )

    return "\n".join(lines)


def write_pr_comment(report: dict, output: str) -> Path:
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_pr_comment(report), encoding="utf-8")
    return output_path


def post_or_update_pr_comment(
    comment_body: str,
    env: Mapping[str, str] | None = None,
) -> dict:
    env = env or os.environ
    token = env.get("GITHUB_TOKEN")
    repository = env.get("GITHUB_REPOSITORY")
    event_path = env.get("GITHUB_EVENT_PATH")
    if not token or not repository or not event_path:
        return {"posted": False, "reason": "missing_environment"}

    pr_number = _pull_request_number(event_path)
    if not pr_number:
        return {"posted": False, "reason": "missing_pull_request"}

    comments_url = f"{GITHUB_API}/repos/{repository}/issues/{pr_number}/comments"
    try:
        comments = _request_json("GET", comments_url, token)
        existing = _existing_relium_comment(comments)
        if existing:
            _request_json("PATCH", existing["url"], token, {"body": comment_body})
            return {"posted": True, "action": "updated", "comment_url": existing.get("html_url")}

        created = _request_json("POST", comments_url, token, {"body": comment_body})
        return {"posted": True, "action": "created", "comment_url": created.get("html_url")}
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        return {"posted": False, "reason": str(exc)}


def _pull_request_number(event_path: str) -> int | None:
    path = Path(event_path)
    if not path.exists():
        return None
    event = json.loads(path.read_text(encoding="utf-8"))
    pull_request = event.get("pull_request") or {}
    return pull_request.get("number") or event.get("number")


def _request_json(method: str, url: str, token: str, payload: dict | None = None):
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "relium-pr-guard")
    if data is not None:
        request.add_header("Content-Type", "application/json")

    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read().decode("utf-8")
    if not body:
        return {}
    return json.loads(body)


def _existing_relium_comment(comments: list[dict]) -> dict | None:
    for comment in comments:
        if MARKER in (comment.get("body") or ""):
            return comment
    return None


def _severity_title(severity: str) -> str:
    return severity.lower().capitalize()


def _sentence(text: str) -> str:
    text = text.rstrip(".")
    return f"{text}."


def _asterisk_lines(items: list[str]) -> list[str]:
    if not items:
        return ["* None found"]
    return [f"* {item}" for item in items]
