import unittest

from agent.api.repository_onboarding import AuthorizedRepository, RepositoryOnboardingService


class _Client:
    def __init__(self):
        self.file_calls = []
        self.branch_calls = []

    def list_installation_repositories(self, token, *, page=1, per_page=100):
        return {"repositories": [{
            "id": 7, "name": "demo", "owner": {"login": "acme"},
            "default_branch": "main", "private": False,
        }], "total_count": 1}

    def get_branch(self, owner, repository, branch):
        self.branch_calls.append((owner, repository, branch))
        return {"name": branch, "commit": {"sha": "new-sha"}}

    def with_token(self, token):
        return self

    def get_file(self, owner, repository, path, ref):
        self.file_calls.append((owner, repository, path, ref))
        return b"name: demo" if path == "dbt_project.yml" else None


class _Store:
    def __init__(self, detection=None):
        self.detection = detection
        self.writes = []

    def tenant_github_installations(self, tenant_id):
        return [{"github_installation_id": 11, "status": "active"}]

    def tenant_repository_detections(self, tenant_id):
        return [] if self.detection is None else [self.detection]

    def upsert_tenant_repository_detection(self, **kwargs):
        self.writes.append(kwargs)

    def tenant_repositories(self, tenant_id):
        return []


class DetectionInvalidationTests(unittest.TestCase):
    def test_null_detection_is_checked_and_persisted_with_head_sha(self):
        client = _Client()
        store = _Store()
        service = RepositoryOnboardingService(
            client=client, jwt_factory=lambda: "jwt",
            installation_token_factory=lambda installation_id: "installation-token",
        )

        repositories = service.list_repositories(store, "tenant")

        self.assertEqual(repositories[0].head_sha, "new-sha")
        self.assertEqual(len(client.file_calls), 1)
        self.assertEqual(store.writes[0]["dbt_checked_commit_sha"], "new-sha")
        self.assertTrue(store.writes[0]["dbt_detected"])

    def test_unchanged_sha_reuses_cached_result_without_contents_request(self):
        client = _Client()
        store = _Store({
            "github_repository_id": 7, "dbt_detected": False,
            "dbt_project_dir": None, "default_branch": "main",
            "dbt_checked_commit_sha": "new-sha",
        })
        service = RepositoryOnboardingService(
            client=client, jwt_factory=lambda: "jwt",
            installation_token_factory=lambda installation_id: "installation-token",
        )

        service.list_repositories(store, "tenant")

        self.assertEqual(client.file_calls, [])
        self.assertEqual(store.writes, [])

    def test_changed_head_rechecks_a_previously_false_result(self):
        client = _Client()
        store = _Store({
            "github_repository_id": 7, "dbt_detected": False,
            "dbt_project_dir": None, "dbt_checked_commit_sha": "old-sha",
        })
        service = RepositoryOnboardingService(
            client=client, jwt_factory=lambda: "jwt",
            installation_token_factory=lambda installation_id: "installation-token",
        )

        service.list_repositories(store, "tenant")

        self.assertEqual(len(client.file_calls), 1)
        self.assertTrue(store.writes[0]["dbt_detected"])


if __name__ == "__main__":
    unittest.main()
