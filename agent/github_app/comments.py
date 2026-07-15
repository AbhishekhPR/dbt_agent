COMMENT_MARKER = "<!-- relium-github-app-review -->"


def upsert_review_comment(client, *, owner: str, repository: str, pull_number: int, body: str):
    marked_body = f"{COMMENT_MARKER}\n{body}"
    comments = client.list_issue_comments(owner, repository, pull_number)
    existing = next(
        (comment for comment in comments if COMMENT_MARKER in str(comment.get("body", ""))),
        None,
    )
    if existing is None:
        return client.create_issue_comment(owner, repository, pull_number, marked_body)
    return client.update_issue_comment(owner, repository, int(existing["id"]), marked_body)
