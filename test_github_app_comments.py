import unittest
from unittest.mock import Mock


class GitHubAppCommentTests(unittest.TestCase):
    def test_creates_comment_when_marker_is_absent(self):
        from agent.github_app.comments import COMMENT_MARKER, upsert_review_comment

        client = Mock()
        client.list_issue_comments.return_value = [{"id": 1, "body": "human"}]
        upsert_review_comment(client, owner="a", repository="r", pull_number=2, body="review")
        client.create_issue_comment.assert_called_once_with("a", "r", 2, f"{COMMENT_MARKER}\nreview")
        client.update_issue_comment.assert_not_called()

    def test_updates_only_the_marked_comment(self):
        from agent.github_app.comments import COMMENT_MARKER, upsert_review_comment

        client = Mock()
        client.list_issue_comments.return_value = [{"id": 7, "body": COMMENT_MARKER + "\nold"}]
        upsert_review_comment(client, owner="a", repository="r", pull_number=2, body="new")
        client.update_issue_comment.assert_called_once_with("a", "r", 7, f"{COMMENT_MARKER}\nnew")
        client.create_issue_comment.assert_not_called()


if __name__ == "__main__":
    unittest.main()
