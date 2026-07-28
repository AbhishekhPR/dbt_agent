import tempfile
import unittest


class GitHubAppStorageTests(unittest.TestCase):
    def test_delivery_claim_is_atomic_and_repository_scoped(self):
        from agent.github_app.storage import RepositoryStorage

        with tempfile.TemporaryDirectory() as tmp:
            store = RepositoryStorage(tmp)
            self.assertTrue(store.claim_delivery(1, "delivery-1"))
            self.assertFalse(store.claim_delivery(1, "delivery-1"))
            self.assertTrue(store.claim_delivery(2, "delivery-1"))

    def test_failed_delivery_can_be_retried_and_completed_delivery_cannot(self):
        from agent.github_app.storage import RepositoryStorage

        with tempfile.TemporaryDirectory() as tmp:
            store = RepositoryStorage(tmp)
            self.assertTrue(store.claim_delivery(1, "retry"))
            store.release_delivery(1, "retry")
            self.assertTrue(store.claim_delivery(1, "retry"))
            store.complete_delivery(1, "retry")
            self.assertFalse(store.claim_delivery(1, "retry"))

    def test_state_is_repository_scoped_and_round_trips(self):
        from agent.github_app.storage import RepositoryStorage

        with tempfile.TemporaryDirectory() as tmp:
            store = RepositoryStorage(tmp)
            store.put(1, "comment-3", {"id": 9})
            self.assertEqual(store.get(1, "comment-3"), {"id": 9})
            self.assertIsNone(store.get(2, "comment-3"))

    def test_rejects_unsafe_identifiers(self):
        from agent.github_app.storage import RepositoryStorage, StorageError

        with tempfile.TemporaryDirectory() as tmp:
            store = RepositoryStorage(tmp)
            with self.assertRaises(StorageError):
                store.claim_delivery(1, "../escape")
            with self.assertRaises(StorageError):
                store.put(0, "key", {})


if __name__ == "__main__":
    unittest.main()
