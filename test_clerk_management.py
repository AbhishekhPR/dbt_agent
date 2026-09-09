from __future__ import annotations

import json
import unittest
import urllib.error

from agent.api.clerk_management import (
    ClerkManagementClient,
    ClerkManagementSettings,
    ClerkMembershipUnavailable,
    ClerkResourceAbsent,
    _RefuseRedirects,
)


class _Response:
    def __init__(self, document, *, status=200):
        self.payload = (b"" if document is None
                        else json.dumps(document).encode("utf-8"))
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, size=-1):
        return self.payload[:size] if size >= 0 else self.payload


class _QueueOpener:
    def __init__(self, documents):
        self.documents = list(documents)
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append((request, timeout))
        if not self.documents:
            raise AssertionError("unexpected Clerk request")
        document = self.documents.pop(0)
        if isinstance(document, Exception):
            raise document
        return _Response(document)


def _membership(identifier, user, role, organization="org_2acme"):
    return {
        "id": identifier,
        "role": role,
        "organization": {"id": organization},
        "public_user_data": {"user_id": user},
        "created_at": 1_780_000_000_000,
        "updated_at": 1_780_000_100_000,
    }


class ClerkManagementSettingsTests(unittest.TestCase):
    def test_secret_is_optional_and_never_rendered(self):
        disabled = ClerkManagementSettings.from_environ({})
        self.assertIsNone(disabled)

        settings = ClerkManagementSettings.from_environ({
            "RELIUM_CLERK_SECRET_KEY": "sk_test_do_not_print",
        })
        self.assertNotIn("sk_test", repr(settings))
        self.assertNotIn("do_not_print", repr(settings))

    def test_management_api_target_cannot_be_redirected_to_an_unsafe_origin(self):
        with self.assertRaises(ValueError):
            ClerkManagementSettings(
                secret_key="sk_test_secret", api_base="http://127.0.0.1/internal")


class ClerkManagementClientTests(unittest.TestCase):
    def test_redirects_are_refused_without_following_another_host(self):
        handler = _RefuseRedirects()
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(
                type("Request", (), {"full_url": "https://api.clerk.com/v1/test"})(),
                None, 302, "Found", {}, "https://attacker.test/")

    def test_fetches_every_membership_page_and_preserves_provenance(self):
        opener = _QueueOpener([
            {"id": "org_2acme", "created_by": "user_owner", "updated_at": 77},
            {"data": [
                _membership("mem_owner", "user_owner", "org:admin"),
                _membership("mem_admin", "user_admin", "org:admin"),
            ], "total_count": 3},
            {"data": [_membership("mem_member", "user_member", "org:member")],
             "total_count": 3},
        ])
        client = ClerkManagementClient(
            ClerkManagementSettings(secret_key="sk_test_secret"),
            opener=opener, page_size=2,
        )

        snapshot = client.organization_snapshot("org_2acme")

        self.assertEqual(snapshot.organization_id, "org_2acme")
        self.assertEqual(snapshot.created_by_user_id, "user_owner")
        self.assertEqual(snapshot.source_version, "77")
        self.assertEqual([m.clerk_membership_id for m in snapshot.memberships],
                         ["mem_owner", "mem_admin", "mem_member"])
        self.assertEqual(snapshot.memberships[0].source_version, "1780000100000")
        self.assertIn("offset=2", opener.requests[-1][0].full_url)
        self.assertEqual(
            opener.requests[0][0].get_header("Authorization"),
            "Bearer sk_test_secret",
        )

    def test_partial_pagination_fails_closed(self):
        opener = _QueueOpener([
            {"id": "org_2acme", "created_by": "user_owner"},
            {"data": [_membership("mem_owner", "user_owner", "org:owner")],
             "total_count": 2},
            {"data": [], "total_count": 2},
        ])
        client = ClerkManagementClient(
            ClerkManagementSettings(secret_key="sk_test_secret"),
            opener=opener, page_size=1,
        )
        with self.assertRaises(ClerkMembershipUnavailable):
            client.organization_snapshot("org_2acme")

    def test_cross_organization_membership_is_rejected(self):
        opener = _QueueOpener([
            {"id": "org_2acme", "created_by": "user_owner"},
            {"data": [_membership(
                "mem_owner", "user_owner", "org:owner", organization="org_other")],
             "total_count": 1},
        ])
        client = ClerkManagementClient(
            ClerkManagementSettings(secret_key="sk_test_secret"), opener=opener)
        with self.assertRaises(ClerkMembershipUnavailable):
            client.organization_snapshot("org_2acme")

    def test_http_failures_are_fail_closed_without_exposing_the_secret(self):
        for status in (401, 403, 429, 500):
            opener = _QueueOpener([urllib.error.HTTPError(
                "https://api.clerk.com/v1/organizations/org_2acme",
                status, "failure", {}, None)])
            client = ClerkManagementClient(
                ClerkManagementSettings(secret_key="sk_test_do_not_expose"),
                opener=opener,
            )
            with self.assertRaises(ClerkMembershipUnavailable) as raised:
                client.organization_snapshot("org_2acme")
            self.assertNotIn("do_not_expose", str(raised.exception))

    def test_membership_without_immutable_provenance_is_rejected(self):
        incomplete = _membership("mem_owner", "user_owner", "org:owner")
        del incomplete["id"]
        opener = _QueueOpener([
            {"id": "org_2acme", "created_by": "user_owner"},
            {"data": [incomplete], "total_count": 1},
        ])
        client = ClerkManagementClient(
            ClerkManagementSettings(secret_key="sk_test_secret"), opener=opener)
        with self.assertRaises(ClerkMembershipUnavailable):
            client.organization_snapshot("org_2acme")

    def test_pagination_total_changing_mid_fetch_is_rejected(self):
        opener = _QueueOpener([
            {"id": "org_2acme", "created_by": "user_owner"},
            {"data": [_membership("mem_owner", "user_owner", "org:owner")],
             "total_count": 2},
            {"data": [_membership("mem_member", "user_member", "org:member")],
             "total_count": 3},
        ])
        client = ClerkManagementClient(
            ClerkManagementSettings(secret_key="sk_test_secret"),
            opener=opener, page_size=1,
        )
        with self.assertRaises(ClerkMembershipUnavailable):
            client.organization_snapshot("org_2acme")

    def test_enumerates_every_authoritative_user_membership(self):
        opener = _QueueOpener([
            {"data": [
                _membership("mem_a", "user_owner", "org:owner", "org_a"),
                _membership("mem_b", "user_owner", "org:member", "org_b"),
            ], "total_count": 3},
            {"data": [
                _membership("mem_c", "user_owner", "org:admin", "org_c"),
            ], "total_count": 3},
        ])
        client = ClerkManagementClient(
            ClerkManagementSettings(secret_key="sk_test_secret"),
            opener=opener, page_size=2,
        )

        memberships = client.user_organization_memberships("user_owner")

        self.assertEqual(
            [(row.organization_id, row.clerk_membership_id, row.clerk_role_key)
             for row in memberships],
            [("org_a", "mem_a", "org:owner"),
             ("org_b", "mem_b", "org:member"),
             ("org_c", "mem_c", "org:admin")],
        )
        self.assertIn("offset=2", opener.requests[-1][0].full_url)

    def test_user_membership_enumeration_rejects_another_user(self):
        opener = _QueueOpener([{
            "data": [_membership("mem_a", "someone_else", "org:member")],
            "total_count": 1,
        }])
        client = ClerkManagementClient(
            ClerkManagementSettings(secret_key="sk_test_secret"), opener=opener)
        with self.assertRaises(ClerkMembershipUnavailable):
            client.user_organization_memberships("user_owner")

    def test_destructive_methods_use_server_selected_resources(self):
        opener = _QueueOpener([{}, {}, {}])
        client = ClerkManagementClient(
            ClerkManagementSettings(secret_key="sk_test_secret"), opener=opener)

        client.delete_organization_membership("org_a", "user_owner")
        client.delete_organization("org_a")
        client.delete_user("user_owner")

        requests = [item[0] for item in opener.requests]
        self.assertEqual([request.method for request in requests],
                         ["DELETE", "DELETE", "DELETE"])
        self.assertTrue(requests[0].full_url.endswith(
            "/organizations/org_a/memberships/user_owner"))
        self.assertTrue(requests[1].full_url.endswith("/organizations/org_a"))
        self.assertTrue(requests[2].full_url.endswith("/users/user_owner"))
        for request in requests:
            self.assertEqual(request.get_header("Authorization"),
                             "Bearer sk_test_secret")

    def test_destructive_404_is_explicit_absence_not_generic_success(self):
        opener = _QueueOpener([urllib.error.HTTPError(
            "https://api.clerk.com/v1/users/user_owner", 404,
            "not found", {}, None)])
        client = ClerkManagementClient(
            ClerkManagementSettings(secret_key="sk_test_secret"), opener=opener)
        with self.assertRaises(ClerkResourceAbsent):
            client.delete_user("user_owner")


if __name__ == "__main__":
    unittest.main()
