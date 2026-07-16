import io
import json
import urllib.error
import unittest


def _http_error(status):
    return urllib.error.HTTPError(
        "https://api.github.com/resource", status, "error", {}, None
    )


class GitHubAppClientTests(unittest.TestCase):
    def test_404_raises_typed_not_found_error(self):
        from agent.github_app.client import GitHubClient, GitHubNotFoundError

        client = GitHubClient(transport=lambda request: (_ for _ in ()).throw(_http_error(404)))
        with self.assertRaises(GitHubNotFoundError) as raised:
            client.get_file("a", "r", "relium.yml", "head")
        self.assertEqual(raised.exception.status_code, 404)

    def test_non_404_http_errors_remain_api_errors(self):
        from agent.github_app.client import GitHubAPIError, GitHubClient, GitHubNotFoundError

        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                client = GitHubClient(
                    transport=lambda request, status=status: (_ for _ in ()).throw(
                        _http_error(status)
                    )
                )
                with self.assertRaises(GitHubAPIError) as raised:
                    client.get_file("a", "r", "relium.yml", "head")
                self.assertNotIsInstance(raised.exception, GitHubNotFoundError)
                self.assertEqual(raised.exception.status_code, status)

    def test_malformed_file_response_fails_clearly(self):
        from agent.github_app.client import GitHubAPIError, GitHubClient

        response = io.BytesIO(json.dumps({"content": "not base64!"}).encode("utf-8"))
        client = GitHubClient(transport=lambda request: response)
        with self.assertRaisesRegex(GitHubAPIError, "file response was invalid"):
            client.get_file("a", "r", "target/manifest.json", "head")

    def test_malformed_json_is_wrapped_without_exposing_response_body(self):
        from agent.github_app.client import GitHubAPIError, GitHubClient

        response = io.BytesIO(b"not-json access-token-secret")
        client = GitHubClient(transport=lambda request: response)
        with self.assertRaisesRegex(GitHubAPIError, "API response was invalid") as raised:
            client.get_file("a", "r", "target/manifest.json", "head")
        self.assertNotIn("access-token-secret", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

    def test_file_response_missing_content_fails_clearly(self):
        from agent.github_app.client import GitHubAPIError, GitHubClient

        response = io.BytesIO(json.dumps({"encoding": "base64"}).encode("utf-8"))
        client = GitHubClient(transport=lambda request: response)
        with self.assertRaisesRegex(GitHubAPIError, "file response was invalid"):
            client.get_file("a", "r", "target/manifest.json", "head")


if __name__ == "__main__":
    unittest.main()
