import unittest
from unittest.mock import Mock


APP_ID = 123


def _comment(comment_id, body, *, app_id=None, created_at="2026-01-01T00:00:00Z"):
    comment = {"id": comment_id, "body": body, "created_at": created_at}
    if app_id is not None:
        comment["performed_via_github_app"] = {"id": app_id, "slug": "relium"}
    return comment


class GitHubAppCommentTests(unittest.TestCase):
    def _upsert(self, comments):
        from agent.github_app.comments import upsert_review_comment

        client = Mock()
        client.list_issue_comments.return_value = comments
        upsert_review_comment(
            client,
            owner="a",
            repository="r",
            pull_number=2,
            body="new",
            expected_app_id=APP_ID,
        )
        return client

    def test_updates_app_owned_marked_comment(self):
        from agent.github_app.comments import COMMENT_MARKER

        client = self._upsert([_comment(7, COMMENT_MARKER + "\nold", app_id=APP_ID)])
        client.update_issue_comment.assert_called_once_with(
            "a", "r", 7, f"{COMMENT_MARKER}\nnew"
        )
        client.create_issue_comment.assert_not_called()

    def test_human_authored_marked_comment_is_ignored(self):
        from agent.github_app.comments import COMMENT_MARKER

        client = self._upsert([_comment(7, COMMENT_MARKER + "\nspoofed")])
        client.update_issue_comment.assert_not_called()
        client.create_issue_comment.assert_called_once_with(
            "a", "r", 2, f"{COMMENT_MARKER}\nnew"
        )

    def test_another_apps_marked_comment_is_ignored(self):
        from agent.github_app.comments import COMMENT_MARKER

        client = self._upsert([_comment(7, COMMENT_MARKER + "\nother", app_id=999)])
        client.update_issue_comment.assert_not_called()
        client.create_issue_comment.assert_called_once()

    def test_no_owned_comment_creates_new_comment(self):
        client = self._upsert([_comment(1, "human")])
        client.update_issue_comment.assert_not_called()
        client.create_issue_comment.assert_called_once()

    def test_newest_owned_marked_comment_is_updated_deterministically(self):
        from agent.github_app.comments import COMMENT_MARKER

        client = self._upsert(
            [
                _comment(
                    7,
                    COMMENT_MARKER + "\nold",
                    app_id=APP_ID,
                    created_at="2026-01-01T00:00:00Z",
                ),
                _comment(
                    9,
                    COMMENT_MARKER + "\nnewer",
                    app_id=APP_ID,
                    created_at="2026-02-01T00:00:00Z",
                ),
            ]
        )
        client.update_issue_comment.assert_called_once_with(
            "a", "r", 9, f"{COMMENT_MARKER}\nnew"
        )


if __name__ == "__main__":
    unittest.main()
