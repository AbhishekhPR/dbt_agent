CHECK_NAME = "Relium deployment review"


def conclusion_for_decision(decision: str, *, enforcement_mode="shadow") -> str:
    normalized = str(decision).lower()
    if normalized in {"allow", "pass", "approved"}:
        return "success"
    if normalized in {"block", "blocked", "deny", "fail", "failed"}:
        return "failure" if enforcement_mode == "enforce" else "neutral"
    return "neutral"


def shadow_mode_result(result: dict, enforcement_mode="shadow") -> dict:
    """The review as published under `enforcement_mode`.

    A shadow-mode check never fails, so a BLOCK is published as WARN with
    `enforce_mode_decision` saying what enforce mode would do: the comment,
    the check title and Slack then agree with the neutral conclusion, exactly
    as the metadata lifecycle publishes a shadow decision. Health, findings
    and the recorded review are unchanged; only the published verdict is.
    """
    shaped = dict(result)
    decision = str(result.get("decision") or "").upper()
    shaped["enforcement_mode"] = enforcement_mode
    shaped["enforce_mode_decision"] = result.get("enforce_mode_decision") or decision
    if enforcement_mode != "enforce" and decision == "BLOCK":
        shaped["decision"] = "WARN"
        if isinstance(result.get("incident"), dict):
            shaped["incident"] = {**result["incident"], "decision": "WARN"}
    return shaped


def build_check_run_payload(
    *,
    head_sha: str,
    result: dict,
    enforcement_mode="shadow",
    external_id=None,
) -> dict:
    markdown = str(result.get("rendered", {}).get("markdown", ""))
    decision = str(result.get("decision", "unknown"))
    payload = {
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
    if external_id:
        payload["external_id"] = str(external_id)
    return payload


def create_review_check(
    client,
    *,
    owner: str,
    repository: str,
    head_sha: str,
    result: dict,
    enforcement_mode="shadow",
    external_id=None,
):
    return client.create_check_run(
        owner,
        repository,
        build_check_run_payload(
            head_sha=head_sha,
            result=result,
            enforcement_mode=enforcement_mode,
            external_id=external_id,
        ),
    )
