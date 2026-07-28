CHECK_NAME = "Relium deployment review"


def conclusion_for_decision(decision: str, *, enforcement_mode="shadow") -> str:
    normalized = str(decision).lower()
    if normalized in {"allow", "pass", "approved"}:
        return "success"
    if normalized in {"block", "blocked", "deny", "fail", "failed"}:
        return "failure" if enforcement_mode == "enforce" else "neutral"
    return "neutral"


def build_check_run_payload(
    *,
    head_sha: str,
    result: dict,
    enforcement_mode="shadow",
) -> dict:
    markdown = str(result.get("rendered", {}).get("markdown", ""))
    decision = str(result.get("decision", "unknown"))
    return {
        "name": CHECK_NAME,
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": conclusion_for_decision(
            decision,
            enforcement_mode=enforcement_mode,
        ),
        "output": {
            "title": f"Relium decision: {decision}",
            "summary": markdown[:65535],
        },
    }


def create_review_check(
    client,
    *,
    owner: str,
    repository: str,
    head_sha: str,
    result: dict,
    enforcement_mode="shadow",
):
    return client.create_check_run(
        owner,
        repository,
        build_check_run_payload(
            head_sha=head_sha,
            result=result,
            enforcement_mode=enforcement_mode,
        ),
    )
