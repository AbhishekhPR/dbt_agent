import base64
import json
import urllib.error
import urllib.parse
import urllib.request


class GitHubAPIError(RuntimeError):
    """Raised when GitHub returns a non-successful API response."""

    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class GitHubNotFoundError(GitHubAPIError):
    """Raised when a requested GitHub resource does not exist."""


# Backwards-compatible name for callers that used the original client error.
GitHubClientError = GitHubAPIError


class GitHubClient:
    """Injectable GitHub API boundary; tests supply a fake instead of using the network."""

    def __init__(self, token=None, *, api_url="https://api.github.com", transport=None):
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.transport = transport or urllib.request.urlopen

    def with_token(self, token):
        return type(self)(token, api_url=self.api_url, transport=self.transport)

    def create_installation_access_token(self, installation_id, app_jwt):
        return self._request("POST", f"/app/installations/{installation_id}/access_tokens", token=app_jwt)

    def get_file(self, owner, repository, path, ref):
        quoted = urllib.parse.quote(path, safe="/")
        response = self._request("GET", f"/repos/{owner}/{repository}/contents/{quoted}?ref={urllib.parse.quote(ref)}")
        if response is None:
            return None
        try:
            return base64.b64decode(response["content"], validate=True)
        except (KeyError, ValueError, TypeError) as exc:
            raise GitHubAPIError("GitHub file response was invalid.") from exc

    def compare_files(self, owner, repository, base_sha, head_sha):
        response = self._request("GET", f"/repos/{owner}/{repository}/compare/{base_sha}...{head_sha}")
        return [item["filename"] for item in response.get("files", [])]

    def list_issue_comments(self, owner, repository, pull_number):
        return self._request("GET", f"/repos/{owner}/{repository}/issues/{pull_number}/comments")

    def create_issue_comment(self, owner, repository, pull_number, body):
        return self._request("POST", f"/repos/{owner}/{repository}/issues/{pull_number}/comments", {"body": body})

    def update_issue_comment(self, owner, repository, comment_id, body):
        return self._request("PATCH", f"/repos/{owner}/{repository}/issues/comments/{comment_id}", {"body": body})

    def create_check_run(self, owner, repository, payload):
        return self._request("POST", f"/repos/{owner}/{repository}/check-runs", payload)

    def _request(self, method, path, data=None, *, token=None):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        credential = self.token if token is None else token
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        request = urllib.request.Request(self.api_url + path, data=body, headers=headers, method=method)
        try:
            with self.transport(request) as response:
                content = response.read()
        except urllib.error.HTTPError as exc:
            error_type = GitHubNotFoundError if exc.code == 404 else GitHubAPIError
            exc.close()
            raise error_type(
                f"GitHub API request failed with status {exc.code}.",
                status_code=exc.code,
            ) from exc
        if not content:
            return {}
        try:
            return json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        raise GitHubAPIError("GitHub API response was invalid.")
