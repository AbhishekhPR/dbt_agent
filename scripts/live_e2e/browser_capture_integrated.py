"""Browser capture of the real relium-app, at one lifecycle point.

Drives the actual dashboard in Chromium. Every value it asserts and every
pixel it captures comes from the running backend: the app has no fixture
fallback, so a screenshot here cannot be of invented state.

It also verifies the property that makes the dashboard "live": the review's
decision must change on screen WITHOUT a page reload. The script loads the
page once per phase run and, when asked to watch, holds it open and waits for
the polled value to change by itself.

Usage:
  python scripts/live_e2e/browser_capture.py --review <id> --label allow \
      --out live-product-evidence/screenshots
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

APP_URL = "http://localhost:5181"


def _text(page, selector, default=None):
    node = page.query_selector(selector)
    return node.inner_text().strip() if node else default


def capture(review_id, label, out_dir, *, app_url=APP_URL, expect_decision=None,
            watch_seconds=0, full_page=True, open_json=True):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    observed = {"label": label, "review_id": review_id, "app_url": app_url}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            # ---- review list -------------------------------------------
            page.goto(f"{app_url}/changes", wait_until="networkidle")
            page.wait_for_selector('.change-table, .change-cards, table', timeout=20000)
            page.screenshot(path=str(out / f"{label}-01-review-list.png"),
                            full_page=full_page)

            # ---- review detail -----------------------------------------
            page.goto(f"{app_url}/changes/{review_id}", wait_until="networkidle")
            page.wait_for_selector('.verdict-word', timeout=20000)

            if watch_seconds and expect_decision:
                # The page is already open. Do NOT reload: the point is that
                # polling brings the new decision in on its own.
                observed["watched_without_reload"] = _watch(
                    page, expect_decision, watch_seconds)

            observed["decision_text"] = _text(page, '.verdict-word')
            observed["lifecycle_state"] = _text(page, '[data-testid="review-identity"] .code-chip')
            observed["health"] = _text(page, '.code-chip')
            observed["coverage"] = _text(page, '.sev')
            observed["finding_codes"] = [
                node.get_attribute("data-code")
                for node in page.query_selector_all('.finding-item, .finding')
            ]
            observed["attempt_rows"] = len(
                page.query_selector_all('[data-testid="attempt-row"]'))
            observed["snapshot_cards"] = len(
                page.query_selector_all('[data-testid="snapshot-card"]'))

            page.screenshot(path=str(out / f"{label}-02-review-detail.png"),
                            full_page=full_page)

            # ---- findings, focused -------------------------------------
            findings = page.query_selector('#findings')
            if findings:
                findings.scroll_into_view_if_needed()
                findings.screenshot(path=str(out / f"{label}-03-findings.png"))

            # ---- attempts / history ------------------------------------
            attempts = page.query_selector('#attempts')
            if attempts:
                attempts.scroll_into_view_if_needed()
                attempts.screenshot(path=str(out / f"{label}-04-attempts.png"))

            # ---- publication -------------------------------------------
            publication = page.query_selector('#metadata')
            if publication:
                publication.scroll_into_view_if_needed()
                publication.screenshot(path=str(out / f"{label}-05-publication.png"))

            # ---- metadata JSON -----------------------------------------
            if open_json:
                loader = page.query_selector('[data-testid="load-snapshot-json"]')
                if loader:
                    loader.click()
                    try:
                        page.wait_for_selector('[data-testid="snapshot-json"]',
                                               timeout=15000)
                        page.click('[data-testid="snapshot-json"] > summary')
                        page.wait_for_timeout(300)
                        node = page.query_selector('[data-testid="snapshot-json"]')
                        node.scroll_into_view_if_needed()
                        node.screenshot(path=str(out / f"{label}-06-metadata-json.png"))
                        observed["metadata_json_visible"] = True
                    except Exception:
                        observed["metadata_json_visible"] = False

            # ---- RCA notice --------------------------------------------
            rca = page.query_selector('#audit')
            if rca:
                rca.scroll_into_view_if_needed()
                rca.screenshot(path=str(out / f"{label}-07-rca-notice.png"))
                observed["rca_not_applicable"] = bool(
                    page.query_selector('[data-testid="rca-not-applicable"]'))
        finally:
            browser.close()

    if expect_decision and expect_decision.lower() not in (
            observed.get("decision_text") or "").lower():
        observed["mismatch"] = (
            f"expected {expect_decision} on screen, saw "
            f"{observed.get('decision_text')!r}")
    return observed


def _watch(page, expect_decision, seconds):
    """Wait for the OPEN page to show the new decision, without reloading."""
    deadline = seconds * 1000
    try:
        page.wait_for_function(
            """(expected) => {
                 const el = document.querySelector('.verdict-word');
                 return el && el.textContent.toLowerCase().includes(expected);
               }""",
            arg=expect_decision.lower(), timeout=deadline)
        return True
    except Exception:
        return False


def capture_blast_radius(review_id, out_dir, *, app_url=APP_URL, pull_number=None,
                         expect_changed_model=None, expect_downstream=(),
                         expect_absent=(), api_origin=None, full_page=True):
    """Capture the blast-radius section of a review, and only that.

    Everything asserted here is read off the rendered page, and the response
    the page actually received is recorded beside it. That pairing is the
    point: a screenshot alone cannot show whether the identities on screen
    came from the API or from somewhere in the frontend, and this proof exists
    precisely because that distinction was in doubt.

    Console errors and failed API responses are collected for the whole
    session rather than sampled, so "no errors" is an observation and not an
    absence of looking.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    expect_downstream = list(expect_downstream)
    expect_absent = list(expect_absent)
    observed = {"review_id": review_id, "app_url": app_url,
                "expected_downstream": expect_downstream}

    console_errors = []
    failed_requests = []
    aborted_requests = []
    api_responses = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1400})

        def on_console(message):
            if message.type in ("error", "warning"):
                console_errors.append({"type": message.type, "text": message.text})

        def on_response(response):
            if api_origin and response.url.startswith(api_origin):
                record = {"url": response.url, "status": response.status}
                if response.status >= 400:
                    failed_requests.append(record)
                api_responses.append(record)

        def on_request_failed(request):
            failure = request.failure or "unknown"
            record = {"url": request.url, "failure": failure}
            # The dashboard polls on an interval and aborts the in-flight
            # request when the page unmounts, so navigating away cancels a
            # poll by design. That is the page stopping its own request, not
            # the API failing one — but it is recorded rather than dropped, so
            # the distinction stays visible instead of being assumed.
            if "ERR_ABORTED" in failure:
                aborted_requests.append(record)
            else:
                failed_requests.append(record)

        page.on("console", on_console)
        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)

        try:
            # ---- queue row -------------------------------------------------
            page.goto(f"{app_url}/changes", wait_until="networkidle")
            page.wait_for_selector("table", timeout=20000)
            page.screenshot(path=str(out / "blast-01-changes-queue.png"),
                            full_page=full_page)
            if pull_number is not None:
                row = page.query_selector(
                    f'tr[aria-label^="Pull request {pull_number},"]')
                observed["queue_row_present"] = bool(row)
                if row:
                    cells = row.query_selector_all("td")
                    observed["queue_downstream_cell"] = (
                        cells[3].inner_text().strip() if len(cells) > 3 else None)

            # ---- the review's blast radius ---------------------------------
            # The response body is taken from the request the page itself
            # made, not from a second call of our own.
            detail_body = {}

            def on_detail(response):
                if api_origin and response.url == f"{api_origin}/api/reviews/{review_id}":
                    try:
                        detail_body["payload"] = response.json()
                    except Exception:  # noqa: BLE001
                        detail_body["payload"] = None

            page.on("response", on_detail)
            page.goto(f"{app_url}/changes/{review_id}", wait_until="networkidle")
            page.wait_for_selector("#blast-radius", timeout=20000)
            section = page.query_selector("#blast-radius")
            section.scroll_into_view_if_needed()

            observed["summary_text"] = _text(section, ".card-head .hint")
            observed["rendered_downstream_ids"] = [
                node.inner_text().strip()
                for node in section.query_selector_all("table.data tbody tr .mono-id")
            ]
            observed["rendered_row_count"] = len(
                section.query_selector_all("table.data tbody tr"))

            graph = section.query_selector('button[aria-label="Graph unavailable"]')
            list_button = section.query_selector('button[aria-label="List"]')
            observed["graph_button_disabled"] = bool(graph and graph.is_disabled())
            observed["list_button_pressed"] = (
                list_button.get_attribute("aria-pressed") if list_button else None)
            observed["unavailable_empty_state"] = bool(
                section.query_selector("text=Blast radius unavailable."))

            section.screenshot(path=str(out / "blast-02-blast-radius.png"))
            page.screenshot(path=str(out / "blast-03-review-report.png"),
                            full_page=full_page)

            changed = page.query_selector('[data-testid="what-changed-facts"]')
            if changed:
                changed.scroll_into_view_if_needed()
                changed.screenshot(path=str(out / "blast-04-what-changed.png"))
                observed["changed_models"] = [
                    node.inner_text().strip()
                    for node in changed.query_selector_all(".code-chip")
                ]

            body_text = page.inner_text("body")
            observed["absent_identities_confirmed"] = {
                identity: identity not in body_text for identity in expect_absent}
            observed["sentinel_text_present"] = any(
                token in body_text for token in ("undefined", "[object Object]"))
            observed["api_response_for_review"] = detail_body.get("payload")
        finally:
            browser.close()

    observed["console_errors"] = console_errors
    observed["failed_api_requests"] = failed_requests
    observed["page_aborted_api_requests"] = aborted_requests
    observed["api_requests_seen"] = api_responses
    observed["api_response_statuses"] = sorted({r["status"] for r in api_responses})

    # ---- verdict on what was observed --------------------------------------
    rendered = observed.get("rendered_downstream_ids") or []
    payload = observed.get("api_response_for_review") or {}
    from_api = ((payload.get("change_plan") or {}).get("downstream_models")) or []
    mismatches = []
    if expect_downstream and rendered != expect_downstream:
        mismatches.append(f"rendered {rendered} != expected {expect_downstream}")
    if from_api and rendered != from_api:
        mismatches.append(f"rendered {rendered} != API {from_api}")
    if expect_changed_model and expect_changed_model not in (
            observed.get("changed_models") or []):
        mismatches.append(f"changed model {expect_changed_model} not rendered")
    if len(rendered) != len(set(rendered)):
        mismatches.append("duplicate downstream identity rendered")
    for identity, absent in (observed.get("absent_identities_confirmed") or {}).items():
        if not absent:
            mismatches.append(f"excluded identity {identity} appeared on screen")
    if observed.get("sentinel_text_present"):
        mismatches.append("sentinel text rendered in user-facing copy")
    if not observed.get("graph_button_disabled"):
        mismatches.append("graph control was not disabled")
    if console_errors:
        mismatches.append(f"{len(console_errors)} console error(s)")
    if failed_requests:
        mismatches.append(f"{len(failed_requests)} failed API request(s)")

    observed["rendered_matches_api"] = bool(from_api) and rendered == from_api
    observed["mismatch"] = "; ".join(mismatches) if mismatches else None
    return observed


def capture_incident(incident_id, out_dir, *, app_url=APP_URL):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    observed = {"incident_id": incident_id}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            page.goto(f"{app_url}/incidents", wait_until="networkidle")
            page.wait_for_selector('[data-testid="incident-table"]', timeout=20000)
            page.screenshot(path=str(out / "incident-01-list.png"), full_page=True)

            page.goto(f"{app_url}/incidents/{incident_id}", wait_until="networkidle")
            page.wait_for_selector('#audit', timeout=20000)
            observed["rca_rendered"] = bool(
                page.query_selector('[data-testid="rca-report"]'))
            page.screenshot(path=str(out / "incident-02-rca.png"), full_page=True)
        finally:
            browser.close()
    return observed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review")
    parser.add_argument("--incident")
    parser.add_argument("--label", default="capture")
    parser.add_argument("--out", required=True)
    parser.add_argument("--expect-decision", default=None)
    parser.add_argument("--watch-seconds", type=int, default=0)
    parser.add_argument("--app-url", default=APP_URL)
    parser.add_argument("--blast-radius", action="store_true",
                        help="capture only the persisted blast-radius section")
    parser.add_argument("--api-origin", default=None,
                        help="API origin, so the page's own responses can be recorded")
    parser.add_argument("--pull-number", type=int, default=None)
    parser.add_argument("--expect-changed-model", default=None)
    parser.add_argument("--expect-downstream", default="",
                        help="comma-separated model identities, in rendered order")
    parser.add_argument("--expect-absent", default="",
                        help="comma-separated identities that must not appear")
    args = parser.parse_args(argv)

    split = lambda raw: [v for v in (s.strip() for s in raw.split(",")) if v]  # noqa: E731

    if args.blast_radius:
        result = capture_blast_radius(
            args.review, args.out, app_url=args.app_url,
            pull_number=args.pull_number,
            expect_changed_model=args.expect_changed_model,
            expect_downstream=split(args.expect_downstream),
            expect_absent=split(args.expect_absent),
            api_origin=args.api_origin)
    elif args.incident:
        result = capture_incident(args.incident, args.out, app_url=args.app_url)
    else:
        result = capture(args.review, args.label, args.out, app_url=args.app_url,
                         expect_decision=args.expect_decision,
                         watch_seconds=args.watch_seconds)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result.get("mismatch") else 0


if __name__ == "__main__":
    raise SystemExit(main())
