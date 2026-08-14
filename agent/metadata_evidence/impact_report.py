"""The canonical per-review impact report, rendered as Markdown.

There is exactly ONE renderer. The in-app report view and the downloadable
`.md` are the same bytes from the same function, because two generators drift
and then the screen and the file disagree about what Relium decided.

Three rules shape everything here.

EVIDENCE ONLY. Every line comes from what the review already persisted. The
renderer is given projections, not a store, so it cannot reach for "the latest"
anything. A section whose evidence is absent is omitted rather than filled with
a plausible sentence - an empty Blast radius section is a fact, an invented one
is a lie.

NON-ATTRIBUTION. Production metadata differences are reported as observations
between two snapshots, never as claims about what the pull request caused. The
comparison runs after the decision and is not an input to it, and the wording
here has to keep saying so: a row-count change between two observations may
have any number of causes, and this report names none of them.

DETERMINISM. No generation timestamp, no request id, no ordering that depends
on dict iteration. The same attempt renders byte-identical Markdown every time,
which is what makes the artifact worth keeping and worth testing.
"""
from __future__ import annotations

DECISION_SUMMARY = {
    "ALLOW": "No production evidence contradicted this change.",
    "WARN": "This change may produce incorrect results in the affected models.",
    "BLOCK": "This change is unsafe to deploy against current production.",
    "NEUTRAL": "Relium reached no decision for this change.",
}

_SEVERITY_ORDER = {"block": 0, "warn": 1, "info": 2}


def _fence(value):
    """Inline code, with backticks in the value neutralised."""
    return f"`{str(value).replace('`', '')}`"


def _short(value, keep=12):
    text = str(value or "")
    return text if len(text) <= keep else f"{text[:keep]}…"


def _heading(lines, text):
    lines.append("")
    lines.append(text)


def _percent(value):
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return None


def _sorted_findings(findings):
    ordered = []
    for index, finding in enumerate(findings or []):
        if not isinstance(finding, dict):
            continue
        severity = str(finding.get("severity") or "info").lower()
        ordered.append((_SEVERITY_ORDER.get(severity, 3), index, finding))
    return [f for _, _, f in sorted(ordered, key=lambda t: (t[0], t[1]))]


def _identity_section(lines, review):
    _heading(lines, "## Review")
    lines.append("")
    rows = [
        ("Review", review.get("review_id")),
        ("Repository", review.get("repository")),
        ("Pull request", f"#{review['pull_number']}"
         if review.get("pull_number") is not None else None),
        ("Environment", review.get("environment")),
        ("Head commit", review.get("head_sha")),
        ("Base commit", review.get("base_sha")),
        ("Attempt", review.get("attempt")),
        ("Lifecycle", review.get("lifecycle_state")),
        ("Enforcement mode", review.get("enforcement_mode")),
    ]
    for label, value in rows:
        if value not in (None, ""):
            lines.append(f"- **{label}:** {_fence(value)}")


def _decision_section(lines, review, attempt):
    decision = (attempt.get("decision") or review.get("decision") or "").upper()
    _heading(lines, "## Decision")
    lines.append("")
    if decision:
        lines.append(f"**{decision}** — {DECISION_SUMMARY.get(decision, '')}".rstrip(" —"))
    else:
        lines.append("No decision has been recorded for this attempt yet.")

    detail = [
        ("Evidence coverage", attempt.get("evidence_coverage")),
        ("Health", attempt.get("health")),
        ("Trigger", attempt.get("trigger")),
    ]
    stated = [f"- **{label}:** {_fence(value)}"
              for label, value in detail if value not in (None, "")]
    if stated:
        lines.append("")
        lines.extend(stated)


def _findings_section(lines, findings):
    ordered = _sorted_findings(findings)
    if not ordered:
        return
    _heading(lines, "## Why Relium flagged it")
    for finding in ordered:
        severity = str(finding.get("severity") or "info").lower()
        code = finding.get("code") or "finding"
        relation = finding.get("relation")
        column = finding.get("column")
        subject = ".".join(part for part in (relation, column) if part)
        lines.append("")
        header = f"### {code} — {severity.upper()}"
        lines.append(header if not subject else f"{header}\n\n{_fence(subject)}")
        message = finding.get("message")
        if message:
            lines.append("")
            lines.append(str(message))
        detail = finding.get("detail")
        if isinstance(detail, dict) and detail:
            lines.append("")
            for key in sorted(detail):
                lines.append(f"- **{key}:** {_fence(detail[key])}")


def _semantic_section(lines, semantic):
    if not isinstance(semantic, dict):
        return
    models = semantic.get("models")
    if not isinstance(models, list) or not models:
        return
    _heading(lines, "## What changed")
    for model in models:
        if not isinstance(model, dict):
            continue
        name = model.get("model_name") or model.get("model_unique_id")
        changes = model.get("changes")
        if not isinstance(changes, list) or not changes:
            continue
        lines.append("")
        lines.append(f"### {name}")
        for change in changes:
            if not isinstance(change, dict):
                continue
            kind = change.get("kind")
            scope = change.get("scope")
            lines.append("")
            label = f"- **{kind}**" if kind else "- change"
            if scope:
                label += f" in {_fence(scope)}"
            lines.append(label)
            before, after = change.get("before_sql"), change.get("after_sql")
            if before in (None, ""):
                lines.append(f"  - before: {_fence('no filter')}"
                             if scope == "where" else "  - before: none recorded")
            else:
                lines.append(f"  - before: {_fence(before)}")
            if after not in (None, ""):
                lines.append(f"  - after: {_fence(after)}")
            unique_id = change.get("model_unique_id")
            if unique_id:
                lines.append(f"  - node: {_fence(unique_id)}")


def _blast_radius_section(lines, change_plan):
    if not isinstance(change_plan, dict):
        return
    edges = change_plan.get("direct_edges")
    downstream = change_plan.get("downstream_models") or []
    changed = change_plan.get("changed_models") or []
    if not (edges or downstream or changed):
        return

    _heading(lines, "## Blast radius")
    lines.append("")
    lines.append("Direct downstream only. Relium does not traverse the graph "
                 "transitively for this report.")

    if changed:
        lines.append("")
        lines.append("**Changed models**")
        lines.append("")
        for model in changed:
            lines.append(f"- {_fence(model)}")

    if isinstance(edges, list):
        lines.append("")
        if edges:
            lines.append("**Direct edges**")
            lines.append("")
            for edge in edges:
                if not isinstance(edge, dict):
                    continue
                source = edge.get("source_model_unique_id")
                target = edge.get("target_model_unique_id")
                if source and target:
                    lines.append(f"- {_fence(source)} → {_fence(target)}")
        else:
            lines.append("No direct downstream models were recorded for this "
                         "change.")
    elif downstream:
        # Legacy review: the targets are known, the source association is not.
        lines.append("")
        lines.append("**Direct downstream models**")
        lines.append("")
        for model in downstream:
            lines.append(f"- {_fence(model)}")
        lines.append("")
        lines.append("Edge evidence was not recorded at analysis time for this "
                     "review, so the source of each dependency is not stated.")


def _production_section(lines, comparison):
    if not isinstance(comparison, dict):
        return
    status = comparison.get("status")
    changes = comparison.get("changes")
    _heading(lines, "## Production metadata")
    lines.append("")
    lines.append(
        "Observed differences between two production snapshots. These are "
        "observations, not attributions: this report does not claim the pull "
        "request caused them.")
    lines.append("")
    for label, key in (("Baseline snapshot", "baseline_snapshot_id"),
                       ("Current snapshot", "current_snapshot_id"),
                       ("Baseline observed", "baseline_observed_at"),
                       ("Current observed", "current_observed_at")):
        value = comparison.get(key)
        if value:
            lines.append(f"- **{label}:** {_fence(value)}")

    if status and status != "evaluated":
        lines.append("")
        lines.append(f"Comparison status: {_fence(status)}.")
        return

    if not isinstance(changes, list) or not changes:
        lines.append("")
        lines.append("No differences were observed between these snapshots.")
        return

    lines.append("")
    lines.append("| Relation | Column | Signal | Before | After |")
    lines.append("| --- | --- | --- | --- | --- |")
    for change in changes:
        if not isinstance(change, dict):
            continue
        signal = change.get("signal") or change.get("kind") or ""
        before, after = change.get("before"), change.get("after")
        if signal == "schema_fingerprint":
            before, after = _short(before), _short(after)
        lines.append(
            f"| {change.get('relation') or ''} | {change.get('column') or ''} "
            f"| {signal} | {before if before is not None else ''} "
            f"| {after if after is not None else ''} |")


def _checks_section(lines, checks):
    rows = [c for c in (checks or []) if isinstance(c, dict)]
    if not rows:
        return
    _heading(lines, "## Validation and checks")
    lines.append("")
    lines.append("| Check | State |")
    lines.append("| --- | --- |")
    for row in sorted(rows, key=lambda r: str(r.get("signal") or r.get("name") or "")):
        name = row.get("signal") or row.get("name") or ""
        state = row.get("state") or row.get("status") or ""
        lines.append(f"| {name} | {state} |")


def _provenance_section(lines, review, attempt, snapshot):
    entries = [
        ("Policy version", attempt.get("policy_version")
         or review.get("policy_version")),
        ("Policy hash", review.get("policy_hash")),
        ("Base manifest hash", review.get("base_manifest_hash")),
        ("Head manifest hash", review.get("head_manifest_hash")),
        ("Snapshot", attempt.get("snapshot_id")),
    ]
    if isinstance(snapshot, dict):
        entries.extend([
            ("Snapshot completeness", snapshot.get("completeness")),
            ("Snapshot freshness", snapshot.get("freshness_state")),
            ("Observed at", snapshot.get("observed_at")),
        ])
    stated = [(label, value) for label, value in entries
              if value not in (None, "")]
    if not stated:
        return
    _heading(lines, "## Evidence provenance")
    lines.append("")
    for label, value in stated:
        lines.append(f"- **{label}:** {_fence(value)}")


def _attempts_section(lines, attempts):
    rows = [a for a in (attempts or []) if isinstance(a, dict)]
    if not rows:
        return
    _heading(lines, "## Attempts")
    lines.append("")
    lines.append("| Attempt | Trigger | Decision | Coverage | Lifecycle |")
    lines.append("| --- | --- | --- | --- | --- |")
    for row in sorted(rows, key=lambda r: r.get("attempt") or 0):
        lines.append(
            f"| {row.get('attempt') or ''} | {row.get('trigger') or ''} "
            f"| {row.get('decision') or '—'} | {row.get('evidence_coverage') or ''} "
            f"| {row.get('lifecycle_state') or ''} |")


def render_review_impact_report(*, review, attempt, findings=(), semantic=None,
                                change_plan=None, comparison=None, checks=(),
                                snapshot=None, attempts=()):
    """Render the canonical impact report for one review attempt.

    Every argument is an already-projected view - the same shapes the API
    returns to the dashboard - so the report cannot show a fact the screen
    cannot. Returns Markdown text with a trailing newline.
    """
    review = review or {}
    attempt = attempt or {}

    lines: list[str] = []
    pull_number = review.get("pull_number")
    title = "# Relium impact report"
    if pull_number is not None:
        title += f" — pull request #{pull_number}"
    lines.append(title)

    _decision_section(lines, review, attempt)
    _findings_section(lines, findings)
    _semantic_section(lines, semantic)
    _blast_radius_section(lines, change_plan)
    _production_section(lines, comparison)
    _checks_section(lines, checks)
    _identity_section(lines, review)
    _provenance_section(lines, review, attempt, snapshot)
    _attempts_section(lines, attempts)

    text = "\n".join(lines).rstrip("\n")
    return text + "\n"


def impact_report_filename(review_id, attempt):
    """A safe, deterministic download name. No path separators can survive."""
    safe = "".join(ch for ch in str(review_id)
                   if ch.isalnum() or ch in {"-", "_"})
    return f"relium-impact-report-{safe}-attempt-{int(attempt)}.md"
