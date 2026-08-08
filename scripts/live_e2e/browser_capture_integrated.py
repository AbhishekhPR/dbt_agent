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
    args = parser.parse_args(argv)

    if args.incident:
        result = capture_incident(args.incident, args.out, app_url=args.app_url)
    else:
        result = capture(args.review, args.label, args.out, app_url=args.app_url,
                         expect_decision=args.expect_decision,
                         watch_seconds=args.watch_seconds)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result.get("mismatch") else 0


if __name__ == "__main__":
    raise SystemExit(main())
