COMMENT_MARKER = "<!-- relium-github-app-review -->"


def upsert_review_comment(
    client,
    *,
    owner: str,
    repository: str,
    pull_number: int,
    body: str,
    expected_app_id: int,
):
    marked_body = f"{COMMENT_MARKER}\n{body}"
    comments = client.list_issue_comments(owner, repository, pull_number)
    owned = [
        comment
        for comment in comments
        if COMMENT_MARKER in str(comment.get("body", ""))
        and _originating_app_id(comment) == expected_app_id
    ]
    existing = max(
        owned,
        key=lambda comment: (str(comment.get("created_at", "")), int(comment["id"])),
        default=None,
    )
    if existing is None:
        return client.create_issue_comment(owner, repository, pull_number, marked_body)
    return client.update_issue_comment(owner, repository, int(existing["id"]), marked_body)


def _originating_app_id(comment):
    app = comment.get("performed_via_github_app")
    if not isinstance(app, dict):
        return None
    app_id = app.get("id")
    if isinstance(app_id, bool) or not isinstance(app_id, int):
        return None
    return app_id
