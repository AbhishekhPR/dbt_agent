"""The real lifecycle worker, as its own process, with recorded transports.

This is ``agent.worker.lifecycle_worker`` unchanged - the same registry, the
same claim/lease/dead-letter semantics, the same handlers. The only thing this
wrapper does is install the publisher the worker uses for republication, built
from the real GitHubSlackPublisher over a recording transport.

Run:
  python scripts/live_e2e/worker_main.py --evidence <dir> [--notify-warn]
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from recording import RecordingGitHubTransport, RecordingSlackOpener  # noqa: E402

from agent.github_app.client import GitHubClient  # noqa: E402
from agent.github_app.slack import SlackPublicationSink  # noqa: E402
from agent.metadata_evidence.publishers import GitHubSlackPublisher  # noqa: E402
from agent.worker import lifecycle_worker  # noqa: E402

APP_ID = 424242


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Live E2E lifecycle worker")
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--owner", default="AbhishekhPR")
    parser.add_argument("--repository", default="relium-e2e-dbt")
    parser.add_argument("--pull-url-template",
                        default="https://github.com/{owner}/{repository}/pull/{pull_number}")
    parser.add_argument("--notify-warn", action="store_true",
                        help="mirrors RELIUM_SLACK_NOTIFY_WARN; the adapter's own rule")
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s %(message)s")

    dsn = os.environ.get("RELIUM_DATABASE_URL", "").strip()
    if not dsn.startswith(("postgresql://", "postgres://")):
        print("RELIUM_DATABASE_URL must be a PostgreSQL DSN", file=sys.stderr)
        return 2

    evidence = Path(args.evidence)
    evidence.mkdir(parents=True, exist_ok=True)

    github_transport = RecordingGitHubTransport(
        evidence / "github-publications.json", app_id=APP_ID)
    slack_opener = RecordingSlackOpener(evidence / "slack-publications.json")

    # Real client, real sink. Only the socket is replaced.
    client = GitHubClient("recorded-installation-token",
                          transport=github_transport)
    sink = SlackPublicationSink(
        "https://hooks.slack.invalid/services/E2E/RECORDED/BOUNDARY",
        notify_warn=args.notify_warn, opener=slack_opener,
        sleep=lambda _s: None)

    def build_publisher(**_scope):
        return GitHubSlackPublisher(
            client, owner=args.owner, repository=args.repository,
            expected_app_id=APP_ID, slack_publisher=sink,
            pull_url_template=args.pull_url_template)

    lifecycle_worker.configure_publisher(build_publisher)

    worker = lifecycle_worker.build_worker(dsn, poll_seconds=args.poll_seconds)

    def _stop(_signum, _frame):
        worker.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, AttributeError, OSError):
            pass

    print(f"worker started; supported events: {lifecycle_worker.registry.supported()}",
          flush=True)
    worker.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
