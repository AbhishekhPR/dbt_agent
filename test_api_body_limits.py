"""Request-size limits for ordinary API writes and manifest evidence."""
from __future__ import annotations

import asyncio
import unittest
from contextlib import contextmanager

from starlette.applications import Starlette
from starlette.testclient import TestClient

from agent.api.auth import AuthenticationError, TenantScope
from agent.api.routes import (
    MAX_API_BODY_BYTES,
    _HttpError,
    _read_json,
    create_api_routes,
)


COMMIT_SHA = "a" * 40
MANIFEST_BODY_LIMIT = 20 * 1024 * 1024


def _manifest_body(size: int) -> bytes:
    prefix = (
        b'{"commit_sha":"' + COMMIT_SHA.encode("ascii")
        + b'","manifest":{"pad":"'
    )
    suffix = b'"}}'
    padding = size - len(prefix) - len(suffix)
    if padding < 0:
        raise ValueError("requested body is too small")
    body = prefix + (b"a" * padding) + suffix
    assert len(body) == size
    return body


class _Store:
    def __init__(self):
        self.persisted = []

    def submit_manifest_evidence(self, organization_id, repository_id, **values):
        self.persisted.append({
            "organization_id": organization_id,
            "repository_id": repository_id,
            "commit_sha": values["commit_sha"],
        })
        return ({
            "evidence_id": "evidence-1",
            "commit_sha": values["commit_sha"],
            "manifest_hash": values["manifest_hash"],
        }, True)


class _Pool:
    def __init__(self, store):
        self.store = store
        self.acquisitions = 0

    @contextmanager
    def acquire(self):
        self.acquisitions += 1
        yield self.store


class _Authenticator:
    def __init__(self, store):
        self.store = store

    def authenticate(self, presented):
        if presented != "ci-test-token":
            raise AuthenticationError("invalid credentials")
        return TenantScope("org-test", "repo-test", scope="ci")


class ApiBodyLimitTests(unittest.TestCase):
    def setUp(self):
        self.store = _Store()
        self.pool = _Pool(self.store)
        app = Starlette(routes=create_api_routes(
            store_pool=self.pool,
            authenticator_factory=_Authenticator,
        ))
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    @property
    def manifest_headers(self):
        return {
            "Authorization": "Bearer ci-test-token",
            "Content-Type": "application/json",
            "Idempotency-Key": "manifest-body-limit-test",
        }

    def test_ordinary_write_above_512_kib_is_rejected(self):
        response = self.client.post(
            "/api/deployments/events",
            content=b"x" * (MAX_API_BODY_BYTES + 1),
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["status"], "payload_too_large")

    def test_manifest_above_512_kib_and_below_20_mib_is_accepted(self):
        response = self.client.post(
            "/api/manifest-evidence",
            content=_manifest_body(600 * 1024),
            headers=self.manifest_headers,
        )

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(len(self.store.persisted), 1)

    def test_manifest_exactly_20_mib_is_accepted(self):
        response = self.client.post(
            "/api/manifest-evidence",
            content=_manifest_body(MANIFEST_BODY_LIMIT),
            headers=self.manifest_headers,
        )

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(len(self.store.persisted), 1)

    def test_manifest_20_mib_plus_one_never_invokes_handler_or_persistence(self):
        response = self.client.post(
            "/api/manifest-evidence",
            content=_manifest_body(MANIFEST_BODY_LIMIT + 1),
            headers=self.manifest_headers,
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["status"], "payload_too_large")
        self.assertEqual(self.pool.acquisitions, 0)
        self.assertEqual(self.store.persisted, [])

    def test_manifest_authorization_behavior_is_unchanged(self):
        response = self.client.post(
            "/api/manifest-evidence",
            content=_manifest_body(1024),
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "unauthorized-manifest",
            },
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["status"], "unauthorized")
        self.assertEqual(self.store.persisted, [])

    def test_manifest_malformed_json_behavior_is_unchanged(self):
        response = self.client.post(
            "/api/manifest-evidence",
            content=b"{not json",
            headers=self.manifest_headers,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "invalid_request")
        self.assertEqual(self.store.persisted, [])


class _StreamingRequest:
    def __init__(self, chunks, *, content_length=None):
        self.headers = {}
        if content_length is not None:
            self.headers["content-length"] = str(content_length)
        self.chunks = chunks
        self.chunks_read = 0

    async def stream(self):
        for chunk in self.chunks:
            self.chunks_read += 1
            yield chunk


class JsonReaderLimitTests(unittest.TestCase):
    def test_oversized_content_length_is_rejected_before_streaming(self):
        request = _StreamingRequest(
            [b'{}'], content_length=MANIFEST_BODY_LIMIT + 1,
        )

        try:
            with self.assertRaises(_HttpError) as raised:
                asyncio.run(_read_json(
                    request, "request-id", max_body_bytes=MANIFEST_BODY_LIMIT,
                ))
        except TypeError as exc:
            self.fail(f"_read_json does not support a per-route limit: {exc}")

        self.assertEqual(raised.exception.status, 413)
        self.assertEqual(request.chunks_read, 0)

    def test_chunked_body_aborts_as_soon_as_route_limit_is_exceeded(self):
        request = _StreamingRequest([b"123", b"456", b"not-read"])

        try:
            with self.assertRaises(_HttpError) as raised:
                asyncio.run(_read_json(request, "request-id", max_body_bytes=5))
        except TypeError as exc:
            self.fail(f"_read_json does not support a per-route limit: {exc}")

        self.assertEqual(raised.exception.status, 413)
        self.assertEqual(request.chunks_read, 2)


if __name__ == "__main__":
    unittest.main()
