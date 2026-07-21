import json
import io
import logging
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch


def _environment(root):
    key_path = Path(root) / "app.pem"
    key_path.write_text("test-private-key", encoding="utf-8")
    return {
        "RELIUM_GITHUB_APP_ID": "123",
        "RELIUM_GITHUB_WEBHOOK_SECRET": "server-webhook-secret",
        "RELIUM_GITHUB_PRIVATE_KEY_PATH": str(key_path),
        "RELIUM_STORAGE_ROOT": str(Path(root) / "storage"),
        "RELIUM_HOST": "127.0.0.1",
        "RELIUM_PORT": "9123",
    }


class GitHubAppServerTests(unittest.TestCase):
    def test_main_loads_settings_and_invokes_injected_server(self):
        from agent.github_app.server import main

        run_server = Mock()
        with tempfile.TemporaryDirectory() as root:
            with patch("agent.github_app.server.configure_logging"):
                main(_environment(root), run_server=run_server)
        app = run_server.call_args.args[0]
        self.assertEqual(run_server.call_args.kwargs["host"], "127.0.0.1")
        self.assertEqual(run_server.call_args.kwargs["port"], 9123)
        self.assertEqual(run_server.call_args.kwargs["lifespan"], "on")
        self.assertFalse(app.state.started)

    def test_main_reports_storage_startup_failure_without_leaking_details(self):
        from agent.github_app.server import main

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as root:
            with (
                patch("agent.github_app.server.configure_logging"),
                patch(
                    "agent.github_app.server.build_application",
                    side_effect=OSError("private-storage-location"),
                ),
                redirect_stderr(output),
            ):
                with self.assertRaises(SystemExit) as raised:
                    main(_environment(root), run_server=Mock())
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("startup error", output.getvalue())
        self.assertNotIn("private-storage-location", output.getvalue())

    def test_build_application_does_not_start_workers(self):
        from agent.github_app.server import build_application
        from agent.github_app.settings import load_settings

        with tempfile.TemporaryDirectory() as root:
            app = build_application(load_settings(_environment(root)))
        self.assertFalse(app.state.started)

    def test_safe_json_formatter_includes_only_allowed_fields(self):
        from agent.github_app.server import SafeJsonFormatter

        record = logging.LogRecord(
            name="relium",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="webhook_processing_failed",
            args=(),
            exc_info=None,
        )
        record.delivery_id = "delivery-1"
        record.event_name = "pull_request"
        record.error_category = "network"
        record.raw_body = "raw-body-secret"
        record.authorization = "Bearer token-secret"
        record.operation = "create_issue_comment"
        record.http_method = "POST"
        record.route_template = "/repos/{owner}/{repo}/issues/{pull_number}/comments"
        record.http_status = 403
        record.github_request_id = "SAFE-REQUEST-ID"
        record.accepted_github_permissions = "issues=write"
        record.github_message_category = "permission"
        record.response_representation = "raw"
        record.retryable = False
        rendered = SafeJsonFormatter().format(record)
        payload = json.loads(rendered)
        self.assertEqual(payload["message"], "webhook_processing_failed")
        self.assertEqual(payload["delivery_id"], "delivery-1")
        self.assertEqual(payload["operation"], "create_issue_comment")
        self.assertEqual(payload["http_status"], 403)
        self.assertEqual(payload["github_request_id"], "SAFE-REQUEST-ID")
        self.assertEqual(payload["accepted_github_permissions"], "issues=write")
        self.assertEqual(payload["github_message_category"], "permission")
        self.assertEqual(payload["response_representation"], "raw")
        self.assertFalse(payload["retryable"])
        self.assertNotIn("raw-body-secret", rendered)
        self.assertNotIn("token-secret", rendered)

    def test_module_import_does_not_load_environment_or_start_server(self):
        import agent.github_app.server as server

        self.assertTrue(callable(server.main))
        self.assertTrue(callable(server.build_application))


if __name__ == "__main__":
    unittest.main()
