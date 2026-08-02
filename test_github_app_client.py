import io
import json
import urllib.error
import urllib.parse
import unittest
from unittest.mock import Mock, patch


def _http_error(status, *, headers=None, body=b""):
    return urllib.error.HTTPError(
        "https://api.github.com/resource",
        status,
        "error",
        headers or {},
        io.BytesIO(body),
    )


class _Response(io.BytesIO):
    def __init__(self, status, value, *, headers=None):
        if isinstance(value, bytes):
            content = value
        elif value is None:
            content = b""
        else:
            content = json.dumps(value).encode("utf-8")
        super().__init__(content)
        self.status = status
        self.headers = headers or {}

    def json(self):
        raise AssertionError("raw repository responses must not be JSON-decoded")


class GitHubAppClientTests(unittest.TestCase):
    def _assert_raw_repository_file(self, body, content_type):
        from agent.github_app.client import GitHubClient

        requests = []

        def transport(request):
            self.assertEqual(
                request.get_header("Accept"),
                "application/vnd.github.raw+json",
            )
            requests.append(request)
            return _Response(
                200,
                body,
                headers={
                    "Content-Type": content_type,
                    "X-GitHub-Media-Type": "github.v3; format=json",
                },
            )

        result = GitHubClient(transport=transport).get_file(
            "a", "r", "target/manifest.json", "head"
        )

        self.assertIsInstance(result, bytes)
        self.assertEqual(result, body)
        self.assertEqual(
            requests[0].get_header("Accept"),
            "application/vnd.github.raw+json",
        )

    def test_raw_yaml_repository_file_returns_unchanged_bytes(self):
        self._assert_raw_repository_file(
            b"manifest_path: target/manifest.json\n", "text/plain"
        )

    def test_raw_json_repository_file_returns_unchanged_bytes(self):
        self._assert_raw_repository_file(
            b'{"metadata":{"dbt_schema_version":"v12"}}', "application/json"
        )

    def test_raw_json_array_repository_file_returns_unchanged_bytes(self):
        self._assert_raw_repository_file(
            b'[{"unique_id":"model.analytics.orders"}]', "application/json"
        )

    def test_raw_sql_repository_file_returns_unchanged_bytes(self):
        self._assert_raw_repository_file(
            b"select * from raw.orders\n", "application/sql"
        )

    def test_empty_repository_file_returns_empty_bytes(self):
        self._assert_raw_repository_file(b"", "text/plain")

    def test_raw_metadata_shaped_json_is_returned_unchanged(self):
        body = (
            b'{"name":"manifest.json","path":"target/manifest.json",'
            b'"content":"not base64!","encoding":"base64"}'
        )
        self._assert_raw_repository_file(body, "application/json")

    def test_raw_request_contract_does_not_guess_from_response_media_type(self):
        from agent.github_app.client import GitHubClient

        body = b"private-response-body"
        result = GitHubClient(
            transport=lambda request: _Response(
                200,
                body,
                headers={"X-GitHub-Media-Type": "github.v3.html; format=html"},
            )
        ).get_file("a", "r", "target/manifest.json", "head")

        self.assertEqual(result, body)

    def test_oversized_repository_file_is_rejected_without_leaking_body(self):
        from agent.github_app.client import GitHubAPIError, GitHubClient

        body = b"private-manifest-content"
        client = GitHubClient(
            transport=lambda request: _Response(200, body),
            max_file_size_bytes=8,
        )

        with self.assertRaisesRegex(GitHubAPIError, "size limit") as raised:
            client.get_file("a", "r", "target/manifest.json", "head")

        error = raised.exception
        self.assertEqual(error.response_representation, "raw")
        self.assertNotIn(body.decode("ascii"), str(error))
        self.assertNotIn(body.decode("ascii"), repr(error.__dict__))

    def test_success_response_contracts_accept_objects_arrays_and_empty_bodies(self):
        from agent.github_app.client import GitHubClient

        cases = (
            (
                _Response(201, {"token": "installation"}),
                lambda client: client.create_installation_access_token(9, "jwt"),
                {"token": "installation"},
            ),
            (
                _Response(200, [{"id": 1}]),
                lambda client: client.list_issue_comments("a", "r", 2),
                [{"id": 1}],
            ),
            (
                _Response(201, {"id": 2}),
                lambda client: client.create_issue_comment("a", "r", 2, "body"),
                {"id": 2},
            ),
            (
                _Response(200, {"id": 2}),
                lambda client: client.update_issue_comment("a", "r", 2, "body"),
                {"id": 2},
            ),
            (
                _Response(201, {"id": 3}),
                lambda client: client.create_check_run("a", "r", {"head_sha": "head"}),
                {"id": 3},
            ),
        )
        for response, invoke, expected in cases:
            with self.subTest(status=response.status, expected=expected):
                client = GitHubClient(transport=lambda request, response=response: response)
                self.assertEqual(invoke(client), expected)

        client = GitHubClient(transport=lambda request: _Response(204, None))
        self.assertEqual(
            client._request(
                "POST",
                "/safe-empty",
                operation="test_empty_success",
                route_template="/safe-empty",
            ),
            {},
        )

    def test_installation_token_is_sent_unchanged_as_bearer(self):
        from agent.github_app.client import GitHubClient

        tokens = (
            "traditional-installation-token",
            "ghs_1234567890_eyJhbGciOiJSUzI1NiJ9.payload.signature",
            "token_with_underscores",
            "token.with.periods",
        )
        for token in tokens:
            with self.subTest(token=token):
                requests = []

                def transport(request):
                    requests.append(request)
                    return _Response(200, [])

                GitHubClient(token=token, transport=transport).list_issue_comments(
                    "a", "r", 2
                )
                self.assertEqual(
                    requests[0].get_header("Authorization"), f"Bearer {token}"
                )

    def test_installation_token_request_inherits_minimal_permissions(self):
        from agent.github_app.client import GitHubClient

        requests = []

        def transport(request):
            requests.append(request)
            return _Response(
                201,
                {
                    "token": "installation-secret",
                    "permissions": {
                        "checks": "write",
                        "contents": "read",
                        "issues": "write",
                        "metadata": "read",
                        "pull_requests": "write",
                    },
                },
            )

        GitHubClient(transport=transport).create_installation_access_token(
            9, "app-jwt-secret"
        )

        self.assertEqual(requests[0].method, "POST")
        self.assertEqual(
            requests[0].selector,
            "/app/installations/9/access_tokens",
        )
        self.assertIsNone(requests[0].data)

    def test_403_preserves_only_safe_operation_diagnostics(self):
        from agent.github_app.client import GitHubAPIError, GitHubClient

        headers = {
            "X-GitHub-Request-Id": "SAFE-REQUEST-ID",
            "X-Accepted-GitHub-Permissions": "issues=write; pull_requests=write",
            "Authorization": "Bearer response-secret",
        }
        client = GitHubClient(
            token="installation-secret",
            transport=lambda request: (_ for _ in ()).throw(
                _http_error(
                    403,
                    headers=headers,
                    body=b'{"message":"token-secret private-response"}',
                )
            ),
        )
        with self.assertRaises(GitHubAPIError) as raised:
            client.create_issue_comment(
                "private-owner", "private-repo", 7, "raw-body-secret"
            )

        error = raised.exception
        self.assertEqual(error.operation, "create_issue_comment")
        self.assertEqual(error.http_method, "POST")
        self.assertEqual(
            error.route_template,
            "/repos/{owner}/{repo}/issues/{pull_number}/comments",
        )
        self.assertEqual(error.status_code, 403)
        self.assertEqual(error.github_request_id, "SAFE-REQUEST-ID")
        self.assertEqual(
            error.accepted_github_permissions,
            "issues=write; pull_requests=write",
        )
        self.assertEqual(error.message_category, "permission")
        self.assertFalse(error.retryable)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        rendered = repr(error.__dict__) + str(error)
        for secret in (
            "installation-secret",
            "response-secret",
            "token-secret",
            "private-response",
            "raw-body-secret",
            "private-owner",
            "private-repo",
        ):
            self.assertNotIn(secret, rendered)

    def test_write_failures_preserve_operation_status_and_retryability(self):
        from agent.github_app.client import GitHubAPIError, GitHubClient

        cases = (
            (403, "comment", "create_issue_comment", False),
            (403, "check", "create_check_run", False),
            (401, "comment", "create_issue_comment", False),
            (429, "comment", "create_issue_comment", True),
            (500, "check", "create_check_run", True),
        )
        for status, kind, operation, retryable in cases:
            with self.subTest(status=status, operation=operation):
                client = GitHubClient(
                    token="opaque-token",
                    transport=lambda request, status=status: (_ for _ in ()).throw(
                        _http_error(status)
                    ),
                )
                with self.assertRaises(GitHubAPIError) as raised:
                    if kind == "comment":
                        client.create_issue_comment("a", "r", 2, "body")
                    else:
                        client.create_check_run("a", "r", {"head_sha": "head"})
                self.assertEqual(raised.exception.operation, operation)
                self.assertEqual(raised.exception.status_code, status)
                self.assertEqual(raised.exception.retryable, retryable)

    def test_404_raises_typed_not_found_error(self):
        from agent.github_app.client import GitHubClient, GitHubNotFoundError

        response_body = b'{"message":"private-file-response"}'
        client = GitHubClient(
            transport=lambda request: (_ for _ in ()).throw(
                _http_error(
                    404,
                    headers={
                        "X-GitHub-Request-Id": "SAFE-REQUEST-ID",
                        "X-Accepted-GitHub-Permissions": "contents=read",
                    },
                    body=response_body,
                )
            )
        )
        with self.assertRaises(GitHubNotFoundError) as raised:
            client.get_file("a", "r", "relium.yml", "head")
        error = raised.exception
        self.assertEqual(error.status_code, 404)
        self.assertEqual(error.operation, "get_repository_file")
        self.assertEqual(error.http_method, "GET")
        self.assertEqual(
            error.route_template, "/repos/{owner}/{repo}/contents/{path}"
        )
        self.assertEqual(error.github_request_id, "SAFE-REQUEST-ID")
        self.assertEqual(error.accepted_github_permissions, "contents=read")
        self.assertEqual(error.message_category, "not_found")
        self.assertEqual(error.response_representation, "raw")
        self.assertFalse(error.retryable)
        self.assertNotIn(response_body.decode("ascii"), str(error))
        self.assertNotIn(response_body.decode("ascii"), repr(error.__dict__))

    def test_non_404_http_errors_remain_api_errors(self):
        from agent.github_app.client import GitHubAPIError, GitHubClient, GitHubNotFoundError

        cases = (
            (401, "authentication", False),
            (403, "permission", False),
            (429, "rate_limit", True),
            (500, "server", True),
        )
        for status, category, retryable in cases:
            with self.subTest(status=status, category=category, retryable=retryable):
                response_body = f'{{"message":"private-file-{status}"}}'.encode()

                def transport(request):
                    raise _http_error(status, body=response_body)

                client = GitHubClient(transport=transport)
                with self.assertRaises(GitHubAPIError) as raised:
                    client.get_file("a", "r", "relium.yml", "head")
                error = raised.exception
                self.assertNotIsInstance(error, GitHubNotFoundError)
                self.assertEqual(error.status_code, status)
                self.assertEqual(error.operation, "get_repository_file")
                self.assertEqual(error.http_method, "GET")
                self.assertEqual(error.message_category, category)
                self.assertEqual(error.retryable, retryable)
                self.assertEqual(error.response_representation, "raw")
                self.assertNotIn(response_body.decode("ascii"), str(error))
                self.assertNotIn(response_body.decode("ascii"), repr(error.__dict__))

    def test_default_transport_uses_explicit_timeout(self):
        from agent.github_app.client import GitHubClient

        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"{}"
        with patch("urllib.request.urlopen", return_value=response) as transport:
            client = GitHubClient(timeout=4.5)
            self.assertEqual(client.list_issue_comments("a", "r", 1), {})
        self.assertEqual(transport.call_args.kwargs, {"timeout": 4.5})

    def test_timeout_must_be_positive(self):
        from agent.github_app.client import GitHubClient

        for timeout in (0, -1, "secret-invalid-timeout"):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(ValueError, "timeout") as raised:
                    GitHubClient(timeout=timeout)
                self.assertNotIn(str(timeout), str(raised.exception))


class GitHubCheckRunPaginationTests(unittest.TestCase):
    check_name = "Relium deployment review"
    publication_id = "review-12-head-sha-shadow"

    @staticmethod
    def _run(run_id, external_id, *, name=None, head_sha="head-sha"):
        return {
            "id": run_id,
            "name": name or "Relium deployment review",
            "head_sha": head_sha,
            "external_id": external_id,
        }

    @staticmethod
    def _page_number(request):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        return int(query.get("page", ["1"])[0])

    def _client_for_pages(self, pages, *, requests=None, create_calls=None):
        from agent.github_app.client import GitHubClient

        requests = requests if requests is not None else []
        create_calls = create_calls if create_calls is not None else []

        def transport(request):
            if request.method == "POST":
                create_calls.append(json.loads(request.data.decode("utf-8")))
                return _Response(201, {"id": 9001})
            page = self._page_number(request)
            requests.append(page)
            return _Response(200, pages[page])

        return GitHubClient(transport=transport)

    def _reconcile(self, client):
        from agent.github_app.runner import PullRequestReviewRunner

        return PullRequestReviewRunner._reconcile_check(
            client,
            owner="acme",
            repository="analytics",
            head_sha="head-sha",
            publication_id=self.publication_id,
            result={"decision": "ALLOW", "rendered": {"markdown": "review"}},
            enforcement_mode="shadow",
            reconcile=True,
        )

    def test_matching_external_id_on_page_two_is_returned(self):
        first = [self._run(index, f"other-{index}") for index in range(100)]
        expected = self._run(101, self.publication_id)
        requests = []
        create_calls = []
        client = self._client_for_pages(
            {
                1: {"total_count": 101, "check_runs": first},
                2: {"total_count": 101, "check_runs": [expected]},
            },
            requests=requests,
            create_calls=create_calls,
        )

        result = self._reconcile(client)

        self.assertEqual(result, expected)
        self.assertEqual(requests, [1, 2])
        self.assertEqual(create_calls, [])

    def test_matching_external_id_on_page_three_is_returned(self):
        pages = {
            1: {
                "total_count": 201,
                "check_runs": [self._run(index, f"p1-{index}") for index in range(100)],
            },
            2: {
                "total_count": 201,
                "check_runs": [self._run(100 + index, f"p2-{index}") for index in range(100)],
            },
            3: {
                "total_count": 201,
                "check_runs": [self._run(201, self.publication_id)],
            },
        }
        requests = []
        create_calls = []
        client = self._client_for_pages(
            pages,
            requests=requests,
            create_calls=create_calls,
        )

        result = self._reconcile(client)

        self.assertEqual(result["external_id"], self.publication_id)
        self.assertEqual(requests, [1, 2, 3])
        self.assertEqual(create_calls, [])

    def test_similar_first_page_checks_do_not_hide_later_exact_identity(self):
        similar = [
            self._run(
                index,
                f"review-12-head-sha-enforce-{index}",
                name="Relium deployment review preview",
            )
            for index in range(100)
        ]
        expected = self._run(101, self.publication_id)
        create_calls = []
        client = self._client_for_pages(
            {
                1: {"total_count": 101, "check_runs": similar},
                2: {"total_count": 101, "check_runs": [expected]},
            },
            create_calls=create_calls,
        )

        result = self._reconcile(client)

        self.assertEqual(result, expected)
        self.assertEqual(create_calls, [])

    def test_reconciliation_requires_name_head_and_mode_specific_external_identity(self):
        from agent.github_app.checks import CHECK_NAME

        publication_id = self.publication_id
        expected = self._run(4, publication_id)

        class ScopedClient:
            def __init__(self):
                self.created = []

            def list_check_runs(self, owner, repository, *, head_sha, check_name):
                self.scope = (owner, repository, head_sha, check_name)
                return [
                    self_run(1, publication_id, name="Relium deployment review preview"),
                    self_run(2, publication_id, head_sha="other-head"),
                    self_run(3, "review-12-head-sha-enforce"),
                    expected,
                ]

            def create_check_run(self, owner, repository, payload):
                self.created.append(payload)
                return {"id": 9001}

        self_run = self._run
        client = ScopedClient()

        result = self._reconcile(client)

        self.assertEqual(result, expected)
        self.assertEqual(client.scope, ("acme", "analytics", "head-sha", CHECK_NAME))
        self.assertEqual(client.created, [])

    def test_no_matching_external_id_creates_exactly_one_check(self):
        create_calls = []
        client = self._client_for_pages(
            {
                1: {
                    "total_count": 101,
                    "check_runs": [self._run(index, f"other-{index}") for index in range(100)],
                },
                2: {"total_count": 101, "check_runs": [self._run(101, "other-final")]},
            },
            create_calls=create_calls,
        )

        result = self._reconcile(client)

        self.assertEqual(result["id"], 9001)
        self.assertEqual(len(create_calls), 1)
        self.assertEqual(create_calls[0]["external_id"], self.publication_id)

    def test_empty_final_page_terminates_pagination(self):
        requests = []
        client = self._client_for_pages(
            {
                1: {
                    "total_count": 201,
                    "check_runs": [self._run(index, f"p1-{index}") for index in range(100)],
                },
                2: {
                    "total_count": 201,
                    "check_runs": [self._run(100 + index, f"p2-{index}") for index in range(100)],
                },
                3: {"total_count": 201, "check_runs": []},
            },
            requests=requests,
        )

        runs = client.list_check_runs(
            "acme", "analytics", head_sha="head-sha", check_name=self.check_name
        )

        self.assertEqual(len(runs), 200)
        self.assertEqual(requests, [1, 2, 3])

    def test_repeated_or_malformed_page_is_rejected_without_looping(self):
        from agent.github_app.client import GitHubAPIError

        first = [self._run(index, f"same-{index}") for index in range(100)]
        cases = (
            {
                1: {"total_count": 201, "check_runs": first},
                2: {"total_count": 201, "check_runs": list(first)},
            },
            {
                1: {"total_count": 101, "check_runs": first},
                2: {"total_count": 101, "check_runs": "not-a-list"},
            },
        )
        for pages in cases:
            with self.subTest(second_page=type(pages[2]["check_runs"]).__name__):
                requests = []
                client = self._client_for_pages(pages, requests=requests)
                with self.assertRaises(GitHubAPIError):
                    client.list_check_runs(
                        "acme",
                        "analytics",
                        head_sha="head-sha",
                        check_name=self.check_name,
                    )
                self.assertEqual(requests, [1, 2])

    def test_later_page_429_uses_bounded_retry_and_restarts_read_safely(self):
        from agent.github_app.jobs import RetryPolicy, RetryingJobProcessor, WebhookJob

        page_calls = []
        later_attempts = 0
        first = [self._run(index, f"other-{index}") for index in range(100)]
        expected = self._run(101, self.publication_id)

        def transport(request):
            nonlocal later_attempts
            page = self._page_number(request)
            page_calls.append(page)
            if page == 2:
                later_attempts += 1
                if later_attempts == 1:
                    raise _http_error(429)
                return _Response(200, {"total_count": 101, "check_runs": [expected]})
            return _Response(200, {"total_count": 101, "check_runs": first})

        from agent.github_app.client import GitHubClient

        client = GitHubClient(transport=transport)
        sleeps = []
        processor = RetryingJobProcessor(
            lambda job: client.list_check_runs(
                "acme", "analytics", head_sha="head-sha", check_name=self.check_name
            ),
            RetryPolicy(max_retries=1, base_seconds=0.01),
            sleep=sleeps.append,
            logger=Mock(),
        )

        runs = processor(WebhookJob("delivery", "pull_request", b"{}", 1.0))

        self.assertEqual(runs[-1], expected)
        self.assertEqual(page_calls, [1, 2, 1, 2])
        self.assertEqual(sleeps, [0.01])

    def test_later_page_5xx_or_timeout_never_creates_during_bounded_failure(self):
        from agent.github_app.client import GitHubAPIError, GitHubClient
        from agent.github_app.jobs import RetryPolicy, RetryingJobProcessor, WebhookJob
        from agent.github_app.runner import PullRequestReviewRunner

        failures = ((_http_error(503), GitHubAPIError), (TimeoutError("temporary"), TimeoutError))
        for failure, expected_error in failures:
            with self.subTest(failure=type(failure).__name__):
                page_calls = []
                create_calls = []
                first = [self._run(index, f"other-{index}") for index in range(100)]

                def transport(request):
                    if request.method == "POST":
                        create_calls.append(request)
                        return _Response(201, {"id": 9001})
                    page = self._page_number(request)
                    page_calls.append(page)
                    if page == 2:
                        if isinstance(failure, urllib.error.HTTPError):
                            raise _http_error(failure.code)
                        raise TimeoutError("temporary timeout")
                    return _Response(200, {"total_count": 101, "check_runs": first})

                client = GitHubClient(transport=transport)
                sleeps = []
                processor = RetryingJobProcessor(
                    lambda job: PullRequestReviewRunner._reconcile_check(
                        client,
                        owner="acme",
                        repository="analytics",
                        head_sha="head-sha",
                        publication_id=self.publication_id,
                        result={"decision": "ALLOW", "rendered": {"markdown": "review"}},
                        enforcement_mode="shadow",
                        reconcile=True,
                    ),
                    RetryPolicy(max_retries=1, base_seconds=0.01),
                    sleep=sleeps.append,
                    logger=Mock(),
                )

                with self.assertRaises(expected_error):
                    processor(WebhookJob("delivery", "pull_request", b"{}", 1.0))

                self.assertEqual(page_calls, [1, 2, 1, 2])
                self.assertEqual(sleeps, [0.01])
                self.assertEqual(create_calls, [])


if __name__ == "__main__":
    unittest.main()
