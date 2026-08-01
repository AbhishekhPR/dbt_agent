import json
import math
import urllib.error
import urllib.parse
import urllib.request


MAX_REPOSITORY_FILE_BYTES = 100 * 1024 * 1024
_RAW_GITHUB_MEDIA_TYPE = "application/vnd.github.raw+json"
_CHECK_RUNS_PER_PAGE = 100
# GitHub limits this endpoint to check runs from the 1,000 most recent check
# suites for a git reference. Ten full pages therefore cover its usable range.
_MAX_CHECK_RUN_PAGES = 10


class ChangedFiles(list):
    """Changed files plus an explicit completeness flag for GitHub compare limits."""

    def __init__(self, values=(), *, complete=True):
        super().__init__(values)
        self.complete = bool(complete)


class GitHubAPIError(RuntimeError):
    """Raised when GitHub returns a non-successful API response."""

    def __init__(
        self,
        message,
        *,
        status_code=None,
        operation=None,
        http_method=None,
        route_template=None,
        github_request_id=None,
        accepted_github_permissions=None,
        message_category=None,
        response_representation=None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.operation = operation
        self.http_method = http_method
        self.route_template = route_template
        self.github_request_id = github_request_id
        self.accepted_github_permissions = accepted_github_permissions
        self.message_category = message_category or _message_category(status_code)
        self.response_representation = response_representation

    @property
    def retryable(self):
        return self.status_code == 429 or (
            isinstance(self.status_code, int) and 500 <= self.status_code <= 599
        )


def safe_github_error_fields(error):
    if not isinstance(error, GitHubAPIError):
        return {}
    return {
        "operation": error.operation,
        "http_method": error.http_method,
        "route_template": error.route_template,
        "http_status": error.status_code,
        "github_request_id": error.github_request_id,
        "accepted_github_permissions": error.accepted_github_permissions,
        "github_message_category": error.message_category,
        "response_representation": error.response_representation,
        "retryable": error.retryable,
    }


class GitHubNotFoundError(GitHubAPIError):
    """Raised when a requested GitHub resource does not exist."""


# Backwards-compatible name for callers that used the original client error.
GitHubClientError = GitHubAPIError


class GitHubClient:
    """Injectable GitHub API boundary; tests supply a fake instead of using the network."""

    def __init__(
        self,
        token=None,
        *,
        api_url="https://api.github.com",
        transport=None,
        timeout=10.0,
        max_file_size_bytes=MAX_REPOSITORY_FILE_BYTES,
    ):
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("GitHub request timeout must be a positive number.")
        if (
            isinstance(max_file_size_bytes, bool)
            or not isinstance(max_file_size_bytes, int)
            or max_file_size_bytes < 0
        ):
            raise ValueError("GitHub repository file size limit must be a non-negative integer.")
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.transport = transport
        self.timeout = float(timeout)
        self.max_file_size_bytes = max_file_size_bytes

    def with_token(self, token):
        return type(self)(
            token,
            api_url=self.api_url,
            transport=self.transport,
            timeout=self.timeout,
            max_file_size_bytes=self.max_file_size_bytes,
        )

    def create_installation_access_token(self, installation_id, app_jwt):
        return self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            token=app_jwt,
            operation="create_installation_token",
            route_template="/app/installations/{installation_id}/access_tokens",
        )

    def get_file(self, owner, repository, path, ref):
        quoted = urllib.parse.quote(path, safe="/")
        return self._request_raw(
            f"/repos/{owner}/{repository}/contents/{quoted}?ref={urllib.parse.quote(ref)}",
            operation="get_repository_file",
            route_template="/repos/{owner}/{repo}/contents/{path}",
        )

    def _request_raw(self, path, *, operation, route_template):
        headers = {
            "Accept": _RAW_GITHUB_MEDIA_TYPE,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            self.api_url + path,
            headers=headers,
            method="GET",
        )
        failure = None
        try:
            response_context = (
                urllib.request.urlopen(request, timeout=self.timeout)
                if self.transport is None
                else self.transport(request)
            )
            with response_context as response:
                response_headers = getattr(response, "headers", None)
                status_code = getattr(response, "status", None)
                content = response.read(self.max_file_size_bytes + 1)
        except urllib.error.HTTPError as exc:
            error_type = GitHubNotFoundError if exc.code == 404 else GitHubAPIError
            failure = (
                error_type,
                exc.code,
                _safe_header(exc.headers, "X-GitHub-Request-Id"),
                _safe_header(exc.headers, "X-Accepted-GitHub-Permissions"),
            )
            exc.close()
        if failure is not None:
            error_type, status_code, request_id, accepted_permissions = failure
            raise error_type(
                f"GitHub API request failed with status {status_code}.",
                status_code=status_code,
                operation=operation,
                http_method="GET",
                route_template=route_template,
                github_request_id=request_id,
                accepted_github_permissions=accepted_permissions,
                response_representation="raw",
            )
        error_fields = {
            "status_code": status_code,
            "operation": operation,
            "http_method": "GET",
            "route_template": route_template,
            "github_request_id": _safe_header(
                response_headers, "X-GitHub-Request-Id"
            ),
            "accepted_github_permissions": _safe_header(
                response_headers, "X-Accepted-GitHub-Permissions"
            ),
            "message_category": "invalid_response",
            "response_representation": "raw",
        }
        if not isinstance(content, bytes):
            raise GitHubAPIError(
                "GitHub repository file response was invalid.", **error_fields
            )
        if len(content) > self.max_file_size_bytes:
            raise GitHubAPIError(
                "GitHub repository file exceeded the configured size limit.",
                **error_fields,
            )
        return bytes(content)

    def compare_files(self, owner, repository, base_sha, head_sha):
        response = self._request(
            "GET",
            f"/repos/{owner}/{repository}/compare/{base_sha}...{head_sha}",
            operation="compare_commits",
            route_template="/repos/{owner}/{repo}/compare/{base}...{head}",
        )
        files = response.get("files", []) if isinstance(response, dict) else []
        if not isinstance(files, list):
            raise GitHubAPIError(
                "GitHub compare response did not contain a file list.",
                operation="compare_commits",
                http_method="GET",
                route_template="/repos/{owner}/{repo}/compare/{base}...{head}",
                message_category="invalid_response",
            )
        names = [
            item["filename"]
            for item in files
            if isinstance(item, dict) and isinstance(item.get("filename"), str)
        ]
        # GitHub's compare endpoint returns at most 300 files. Exactly 300 is
        # therefore explicitly treated as potentially truncated.
        return ChangedFiles(names, complete=len(files) < 300)

    def list_issue_comments(self, owner, repository, pull_number):
        path = f"/repos/{owner}/{repository}/issues/{pull_number}/comments"
        first = self._request(
            "GET",
            path,
            operation="list_issue_comments",
            route_template="/repos/{owner}/{repo}/issues/{pull_number}/comments",
        )
        if not isinstance(first, list):
            return first
        comments = list(first)
        page = 2
        while len(first) == 100:
            page_result = self._request(
                "GET",
                f"{path}?page={page}&per_page=100",
                operation="list_issue_comments",
                route_template="/repos/{owner}/{repo}/issues/{pull_number}/comments",
            )
            if not isinstance(page_result, list):
                break
            comments.extend(page_result)
            first = page_result
            page += 1
        return comments

    def create_issue_comment(self, owner, repository, pull_number, body):
        return self._request(
            "POST",
            f"/repos/{owner}/{repository}/issues/{pull_number}/comments",
            {"body": body},
            operation="create_issue_comment",
            route_template="/repos/{owner}/{repo}/issues/{pull_number}/comments",
        )

    def update_issue_comment(self, owner, repository, comment_id, body):
        return self._request(
            "PATCH",
            f"/repos/{owner}/{repository}/issues/comments/{comment_id}",
            {"body": body},
            operation="update_issue_comment",
            route_template="/repos/{owner}/{repo}/issues/comments/{comment_id}",
        )

    def create_check_run(self, owner, repository, payload):
        return self._request(
            "POST",
            f"/repos/{owner}/{repository}/check-runs",
            payload,
            operation="create_check_run",
            route_template="/repos/{owner}/{repo}/check-runs",
        )

    def list_check_runs(self, owner, repository, *, head_sha, check_name):
        """List existing runs so an ambiguous publication can be reconciled."""
        route_template = "/repos/{owner}/{repo}/commits/{ref}/check-runs"
        path = f"/repos/{owner}/{repository}/commits/{head_sha}/check-runs"
        encoded_name = urllib.parse.quote(str(check_name), safe="")
        check_runs = []
        seen_pages = set()
        expected_total = 0
        for page in range(1, _MAX_CHECK_RUN_PAGES + 1):
            response = self._request(
                "GET",
                (
                    f"{path}?check_name={encoded_name}&filter=all"
                    f"&per_page={_CHECK_RUNS_PER_PAGE}&page={page}"
                ),
                operation="list_check_runs",
                route_template=route_template,
            )
            if not isinstance(response, dict):
                raise _invalid_check_run_page(route_template)
            page_runs = response.get("check_runs")
            total_count = response.get("total_count")
            if (
                not isinstance(page_runs, list)
                or isinstance(total_count, bool)
                or not isinstance(total_count, int)
                or total_count < 0
            ):
                raise _invalid_check_run_page(route_template)
            if not page_runs:
                return check_runs
            fingerprint = json.dumps(
                page_runs,
                sort_keys=True,
                separators=(",", ":"),
            )
            if fingerprint in seen_pages:
                raise GitHubAPIError(
                    "GitHub check-run pagination repeated a page.",
                    operation="list_check_runs",
                    http_method="GET",
                    route_template=route_template,
                    message_category="invalid_response",
                )
            seen_pages.add(fingerprint)
            check_runs.extend(page_runs)
            expected_total = max(expected_total, total_count)
            if len(check_runs) >= expected_total:
                return check_runs
        raise GitHubAPIError(
            "GitHub check-run pagination exceeded the supported endpoint limit.",
            operation="list_check_runs",
            http_method="GET",
            route_template=route_template,
            message_category="invalid_response",
        )

    def _request(
        self,
        method,
        path,
        data=None,
        *,
        token=None,
        operation=None,
        route_template=None,
    ):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        credential = self.token if token is None else token
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        request = urllib.request.Request(self.api_url + path, data=body, headers=headers, method=method)
        failure = None
        try:
            response_context = (
                urllib.request.urlopen(request, timeout=self.timeout)
                if self.transport is None
                else self.transport(request)
            )
            with response_context as response:
                content = response.read()
        except urllib.error.HTTPError as exc:
            error_type = GitHubNotFoundError if exc.code == 404 else GitHubAPIError
            request_id = _safe_header(exc.headers, "X-GitHub-Request-Id")
            accepted_permissions = _safe_header(
                exc.headers, "X-Accepted-GitHub-Permissions"
            )
            failure = (
                error_type,
                exc.code,
                request_id,
                accepted_permissions,
            )
            exc.close()
        if failure is not None:
            error_type, status_code, request_id, accepted_permissions = failure
            raise error_type(
                f"GitHub API request failed with status {status_code}.",
                status_code=status_code,
                operation=operation,
                http_method=method,
                route_template=route_template,
                github_request_id=request_id,
                accepted_github_permissions=accepted_permissions,
            )
        if not content:
            return {}
        try:
            return json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        raise GitHubAPIError(
            "GitHub API response was invalid.",
            status_code=getattr(response, "status", None),
            operation=operation,
            http_method=method,
            route_template=route_template,
            github_request_id=_safe_header(
                getattr(response, "headers", None), "X-GitHub-Request-Id"
            ),
            message_category="invalid_response",
        )


def _safe_header(headers, name):
    if headers is None:
        return None
    value = headers.get(name)
    if not isinstance(value, str):
        return None
    sanitized = value.replace("\r", "").replace("\n", "").strip()
    return sanitized[:500] or None


def _invalid_check_run_page(route_template):
    return GitHubAPIError(
        "GitHub check-run response did not contain a valid total_count and check_runs list.",
        operation="list_check_runs",
        http_method="GET",
        route_template=route_template,
        message_category="invalid_response",
    )


def _message_category(status_code):
    if status_code == 401:
        return "authentication"
    if status_code == 403:
        return "permission"
    if status_code == 404:
        return "not_found"
    if status_code == 429:
        return "rate_limit"
    if isinstance(status_code, int) and 500 <= status_code <= 599:
        return "server"
    if status_code == 422:
        return "validation"
    return "api_error"
