"""One-time recovery and focused cleanup proof for the dedicated E2E App.

This is deliberately not a product E2E.  It creates no pull request, branch,
review, database, API, or worker.  The only outward mutation is the dedicated
``relium-e2e`` App webhook URL, performed with the App JWT on an ephemeral
GitHub Actions runner.

The recovery anchors are persisted evidence, not a guessed endpoint:

* metadata-review Run 31085032785 preserved and restored
  ``https://example.invalid/github/webhook`` with both
  ``matches_original`` and ``verified_through_github`` true;
* governance Run 31246080645 repointed the webhook to
  ``https://connector-wind-terms-yet.trycloudflare.com/github/webhook`` and
  its cleanup recorded ``no recovery record`` and ``restored`` false.

Neither path reads, writes, prints, or persists the webhook secret.
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import live_flow as lf  # noqa: E402
import metadata_review_e2e as md  # noqa: E402
from live_flow import StageFailure  # noqa: E402
from metadata_review_e2e import (  # noqa: E402
    app_jwt, gh, installation_token, preserve_webhook, restore_webhook,
)

EVIDENCE_DIR = Path(sys.argv[1])
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

APP_SLUG = "relium-e2e"
FIXTURE_REPOSITORY = "AbhishekhPR/relium-e2e-dbt"
RUN_11 = 31085032785
CORRUPTING_RUN = 31246080645
ORIGINAL_URL = "https://example.invalid/github/webhook"
CORRUPT_URL = (
    "https://connector-wind-terms-yet.trycloudflare.com/github/webhook")
ORIGINAL_CONTENT_TYPE = "json"


def _write(name: str, value: dict) -> None:
    (EVIDENCE_DIR / name).write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise StageFailure(message)


def read_app_identity(jwt: str | None = None) -> dict:
    """Read only identity/subscription fields, before any webhook metadata."""
    jwt = jwt or app_jwt()
    app_status, app = gh("GET", "/app", jwt)
    _require(app_status == 200, f"could not read App identity: HTTP {app_status}")
    return {"app_slug": app.get("slug"),
            "events": sorted(app.get("events") or [])}


def read_webhook_state(jwt: str | None = None, identity: dict | None = None) -> dict:
    """Read non-secret config only after the App identity has passed its gate."""
    jwt = jwt or app_jwt()
    identity = identity or read_app_identity(jwt)
    _assert_dedicated_app(identity)
    hook_status, hook = gh("GET", "/app/hook/config", jwt)
    hook.pop("secret", None)
    _require(hook_status == 200,
             f"could not read App webhook config: HTTP {hook_status}")
    return {
        **identity,
        "active": None,
        "url": hook.get("url"),
        "content_type": hook.get("content_type"),
        "insecure_ssl": hook.get("insecure_ssl"),
    }


def _assert_dedicated_app(state: dict) -> None:
    slug = state.get("app_slug") or ""
    _require(slug == APP_SLUG and "pilot" not in slug.lower(),
             f"refusing webhook operation for App slug {slug!r}")
    _require("pull_request" in state.get("events", []),
             "the dedicated E2E App is not subscribed to pull_request")


def _assert_installation_scope(jwt: str) -> list[str]:
    token = installation_token(jwt)
    status, response = gh("GET", "/installation/repositories", token,
                          bearer=False)
    names = sorted(repo["full_name"] for repo in response.get("repositories", []))
    _require(status == 200 and names == [FIXTURE_REPOSITORY],
             f"dedicated App installation repository mismatch: {names}")
    return names


def _patch_webhook(url: str, content_type: str) -> int:
    status, _ = gh("PATCH", "/app/hook/config", app_jwt(),
                   {"url": url, "content_type": content_type})
    _require(status == 200, f"webhook update failed: HTTP {status}")
    return status


class _ProbeHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/healthz":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):  # noqa: N802
        self.send_response(202 if self.path == "/github/webhook" else 404)
        self.end_headers()

    def log_message(self, _format, *_args):
        return


def _start_probe_listener():
    server = ThreadingHTTPServer(("127.0.0.1", lf.PORT), _ProbeHandler)
    thread = threading.Thread(target=server.serve_forever,
                              name="webhook-recovery-probe", daemon=True)
    thread.start()
    return server, thread


def _stop_tunnel_and_listener(server, thread) -> dict:
    tunnel = md.state.get("tunnel")
    if tunnel:
        proc = tunnel["proc"]
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            proc.kill()
            proc.wait(timeout=15)
    server.shutdown()
    server.server_close()
    thread.join(timeout=10)
    listener_up = False
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{lf.PORT}/healthz", timeout=2)
        listener_up = True
    except (urllib.error.URLError, OSError):
        pass
    return {
        "tunnel_stopped": bool(tunnel and tunnel["proc"].poll() is not None),
        "local_listener_still_up": listener_up,
    }


def _fixture_state() -> dict:
    token = installation_token()
    pulls = md.gh_list_all(
        f"/repos/{FIXTURE_REPOSITORY}/pulls?state=open", token,
        bearer=False)
    branches = md.gh_list_all(
        f"/repos/{FIXTURE_REPOSITORY}/branches", token, bearer=False)
    return {"open_fixture_pull_requests": [pull["number"] for pull in pulls],
            "fixture_branches_remaining": [
                branch["name"] for branch in branches
                if branch["name"] != "main"]}


def _same_untouched_fields(left: dict, right: dict) -> bool:
    return all(left.get(field) == right.get(field)
               for field in ("events", "insecure_ssl"))


def main() -> int:
    jwt = app_jwt()
    identity = read_app_identity(jwt)
    _assert_dedicated_app(identity)
    repositories = _assert_installation_scope(jwt)
    observed = read_webhook_state(jwt, identity)
    _write("webhook-state-observed-before-mutation.json", {
        "observed": observed,
        "secret_captured": False,
        "github_mutation_performed": False,
    })
    _require(observed["url"] in (CORRUPT_URL, ORIGINAL_URL),
             "current webhook URL matches neither the proven corrupt URL nor "
             "the proven Run 11 original")
    _require(observed["content_type"] == ORIGINAL_CONTENT_TYPE,
             "current content type differs from the Run 11 original")

    recovery_patch_status = None
    if observed["url"] == CORRUPT_URL:
        recovery_patch_status = _patch_webhook(ORIGINAL_URL,
                                               ORIGINAL_CONTENT_TYPE)

    original = read_webhook_state()
    _assert_dedicated_app(original)
    _require(original["url"] == ORIGINAL_URL,
             "one-time recovery did not restore the Run 11 URL")
    _require(original["content_type"] == ORIGINAL_CONTENT_TYPE,
             "one-time recovery did not restore the Run 11 content type")
    _require(_same_untouched_fields(observed, original),
             "events/TLS state changed during one-time recovery")

    server, thread = _start_probe_listener()
    restored = False
    cleanup = {}
    try:
        lf.start_tunnel(md.state, EVIDENCE_DIR / "tunnel.log")
        preserve_webhook()
        preserved = json.loads(md.RECOVERY.read_text(encoding="utf-8"))
        _require(preserved.get("url") == ORIGINAL_URL,
                 "temporary proof did not preserve the recovered original")
        _require(preserved.get("content_type") == ORIGINAL_CONTENT_TYPE,
                 "temporary proof preserved the wrong content type")

        tunnel_url = md.state["tunnel"]["url"]
        temporary_url = tunnel_url.rstrip("/") + "/github/webhook"
        lf.point_webhook(md.state, gh, app_jwt, tunnel_url)
        lf.verify_webhook(gh, app_jwt, tunnel_url)
        temporary = read_webhook_state()
        _require(temporary["url"] == temporary_url,
                 "GitHub did not retain the temporary webhook URL")
        _require(temporary_url != ORIGINAL_URL,
                 "temporary webhook URL unexpectedly equals the original")
        _require(_same_untouched_fields(original, temporary),
                 "events/TLS state changed during temporary mutation")

        restoration = restore_webhook()
        _require(restoration.get("verified_through_github") is True,
                 "helper restoration was not verified through GitHub")
        final = read_webhook_state()
        _require(final["url"] == ORIGINAL_URL,
                 "final webhook URL does not equal the recovered original")
        _require(final["content_type"] == ORIGINAL_CONTENT_TYPE,
                 "final content type does not equal the recovered original")
        _require(_same_untouched_fields(original, final),
                 "final events/TLS state differs from the original")
        restored = True
    finally:
        if md.state.get("mutated") and not restored:
            restore_webhook()
        cleanup = _stop_tunnel_and_listener(server, thread)

    fixtures = _fixture_state()
    _require(not fixtures["open_fixture_pull_requests"],
             f"fixture pull requests remain open: {fixtures['open_fixture_pull_requests']}")
    _require(not fixtures["fixture_branches_remaining"],
             f"fixture branches remain: {fixtures['fixture_branches_remaining']}")
    _require(cleanup["tunnel_stopped"],
             "disposable E2E tunnel remains after cleanup")
    _require(not cleanup["local_listener_still_up"],
             "local E2E listener remains after cleanup")

    evidence = {
        "evidence_type": "dedicated-e2e-webhook-recovery-and-cleanup-proof",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "authoritative_sources": {
            "original_configuration_run": RUN_11,
            "corrupting_governance_run": CORRUPTING_RUN,
        },
        "one_time_recovery": {
            "observed_url": observed["url"],
            "expected_corrupt_url": CORRUPT_URL,
            "recovery_patch_status": recovery_patch_status,
            "recovered_url": original["url"],
            "recovered_content_type": original["content_type"],
        },
        "cleanup_regression_proof": {
            "original_url_before_temporary_mutation": original["url"],
            "temporary_url": temporary["url"],
            "temporary_url_is_different": temporary["url"] != original["url"],
            "final_url": final["url"],
            "final_url_equals_original": final["url"] == original["url"],
            "content_type": {
                "original": original["content_type"],
                "temporary": temporary["content_type"],
                "final": final["content_type"],
                "matches": (original["content_type"] == temporary["content_type"]
                            == final["content_type"]),
            },
            # The GitHub App REST API does not expose active. Successful
            # genuine deliveries plus the narrow PATCH field set are the
            # authoritative evidence for this registration-only setting.
            "active_state": {
                "intended": True,
                "matches": True,
                "verification_basis": (
                    "Runs 31085032785 and 31246080645 received genuine "
                    "pull_request deliveries; the corrupting PATCH changed "
                    "only url/content_type. The GitHub App REST API does not "
                    "expose active, so no unavailable response field is "
                    "treated as runtime proof."),
            },
            "events": {"original": original["events"],
                       "temporary": temporary["events"],
                       "final": final["events"],
                       "matches": (original["events"] == temporary["events"]
                                   == final["events"])},
            "insecure_ssl": {"original": original["insecure_ssl"],
                             "temporary": temporary["insecure_ssl"],
                             "final": final["insecure_ssl"],
                             "matches": (original["insecure_ssl"]
                                         == temporary["insecure_ssl"]
                                         == final["insecure_ssl"])},
            "verified_through_github": True,
        },
        "identity": {
            "app_slug": final["app_slug"],
            "installation_repositories": repositories,
            "relium_pilot_touched": False,
        },
        "cleanup": {**fixtures, **cleanup},
        "security": {
            "secret_captured": False,
            "secret_touched": False,
            "fixture_token_used_for_webhook_administration": False,
            "private_key_written_to_evidence": False,
        },
    }
    _write("webhook-recovery-cleanup-proof.json", evidence)
    print("dedicated E2E webhook recovery and cleanup proof passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
