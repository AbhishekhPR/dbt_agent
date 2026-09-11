"""What the logs say when GitHub is the thing that failed.

``github_unavailable`` is one code covering several unrelated failures — the
installation token, the repository listing, the pagination safety bound, the
default branch lookup and the dbt probe — and every one of them is re-raised
``from None``, which is right for the browser and useless for whoever is on
call. These tests are the contract for the instrumentation that closes that
gap, and for the two things it must not disturb on its way past.

    THE OUTWARD CONTRACT IS UNCHANGED. Every test that asserts a log line also
    asserts the caller still gets exactly ``github_unavailable`` with no
    detail and no chained cause. Instrumentation that alters what a customer
    sees is not instrumentation.

    NOTHING SENSITIVE REACHES A LOG. The fakes below authenticate with a
    sentinel string and GitHub is scripted to put that same string inside its
    error message — the worst realistic case, a provider echoing a credential
    back in an error body. Every test then asserts the sentinel is absent from
    every field of every record.

The last class covers the other half of the same fault: ONE repository whose
default branch GitHub cannot resolve must degrade on its own rather than
collapsing the listing, and must do it without borrowing the fail-closed event.

NO REAL CREDENTIAL APPEARS IN THIS FILE. `SENTINEL_TOKEN` is a marker chosen to
be greppable and obviously fake; it is never sent anywhere.

No PostgreSQL: this is the service object, its fakes, and `assertLogs`.
"""
from __future__ import annotations

import ast
import logging
import pathlib
import unittest

from agent.api.repository_onboarding import (
    CODE_GITHUB_UNAVAILABLE, EVENT_BRANCH_UNRESOLVED, EVENT_GITHUB_UNAVAILABLE,
    AuthorizedRepository, RepositoryOnboardingError, RepositoryOnboardingService,
)

LOGGER_NAME = "agent.api.repository_onboarding"

#: Stands in for an installation token. Deliberately unmistakable, so a test
#: failure reads as "a credential reached a log" rather than as a diff.
SENTINEL_TOKEN = "ghs_SENTINEL_NOT_A_REAL_TOKEN_000000000000"
#: Stands in for an app JWT on the same principle.
SENTINEL_JWT = "eyJSENTINEL.NOT_A_REAL_JWT.SIGNATURE"

INSTALLATION = 111111
OWNER = "acme-analytics"
REPOSITORY = "analytics"
BRANCH = "main"

REPO_PAYLOAD = {
    "id": 900001, "name": REPOSITORY, "private": True,
    "default_branch": BRANCH, "owner": {"login": OWNER},
}

#: The repository from the production log line that started this: authorized,
#: listed, and with no default branch GitHub will admit to.
EMPTY_REPOSITORY = "youtube-skeleton-clone"
EMPTY_REPO_PAYLOAD = {
    "id": 900002, "name": EMPTY_REPOSITORY, "private": True,
    "default_branch": BRANCH, "owner": {"login": OWNER},
}
TWO_REPOSITORIES = {
    "total_count": 2, "repositories": [REPO_PAYLOAD, EMPTY_REPO_PAYLOAD],
}

ACME_REPOSITORY = AuthorizedRepository(
    github_repository_id=900001, owner_login=OWNER, name=REPOSITORY,
    full_name=f"{OWNER}/{REPOSITORY}", default_branch=BRANCH, private=True,
    installation_id=INSTALLATION,
)


def github_error(message=None, **kwargs):
    """A GitHubAPIError whose message carries the sentinel, as a real one might.

    GitHub's 401 body is literally "Bad credentials"; a proxy or a future
    endpoint echoing the presented token back is not far-fetched. Building the
    worst case into every fixture is cheaper than hoping.
    """
    from agent.github_app.client import GitHubAPIError

    return GitHubAPIError(
        message or f"Bad credentials: token {SENTINEL_TOKEN} was rejected",
        **kwargs)


class _FakeClient:
    """A scripted GitHub. Each call either answers or raises what it was told."""

    def __init__(self, *, list_error=None, list_document=None,
                 branch_error=None, branch_document=None, file_error=None,
                 never_completes=False, branch_errors=None):
        self.list_error = list_error
        self.list_document = (list_document if list_document is not None
                              else {"total_count": 1, "repositories": [REPO_PAYLOAD]})
        self.branch_error = branch_error
        # Per-repository outcomes, keyed by name. This is what lets a single
        # repository fail while its neighbours answer normally — the shape of
        # the production fault.
        self.branch_errors = dict(branch_errors or {})
        self.branch_document = (branch_document if branch_document is not None
                                else {"commit": {"sha": "a" * 40}})
        self.file_error = file_error
        #: Every (repository, path) the dbt probe asked for. Proving a call was
        #: NOT made needs a record of the ones that were.
        self.get_file_calls = []
        # Advertises far more than it ever hands over, so the paging loop can
        # never satisfy its exit condition and must hit the safety bound.
        self.never_completes = never_completes
        self.token = None

    def with_token(self, token):
        clone = _FakeClient.__new__(_FakeClient)
        clone.__dict__.update(self.__dict__)
        clone.token = token
        return clone

    def list_installation_repositories(self, token, *, page=1, per_page=100):
        if self.list_error is not None:
            raise self.list_error
        if self.never_completes:
            return {"total_count": 999_999, "repositories": [REPO_PAYLOAD]}
        return self.list_document if page == 1 else {"repositories": []}

    def get_branch(self, owner, repository, branch):
        if repository in self.branch_errors:
            raise self.branch_errors[repository]
        if self.branch_error is not None:
            raise self.branch_error
        return self.branch_document

    def get_file(self, owner, repository, path, ref):
        self.get_file_calls.append((repository, path))
        if self.file_error is not None:
            raise self.file_error
        return None


class _FakeStore:
    def tenant_github_installations(self, tenant_id):
        return [{"github_installation_id": INSTALLATION, "status": "active"}]


class _RecordingStore(_FakeStore):
    """A store that also offers the optional detection-cache surface.

    `list_repositories` probes for these with `hasattr`, so the plain fake
    above exercises the path where they are absent. This one exercises the
    path where they exist, and remembers what was written.
    """

    def __init__(self, detections=()):
        self.detections = list(detections)
        self.upserts = []

    def tenant_repository_detections(self, tenant_id):
        return self.detections

    def upsert_tenant_repository_detection(self, **kwargs):
        self.upserts.append(kwargs)


def build_service(client, *, token_factory=None):
    return RepositoryOnboardingService(
        client=client,
        jwt_factory=lambda: SENTINEL_JWT,
        installation_token_factory=token_factory or (lambda _id: SENTINEL_TOKEN),
    )


def _real_token_path(service, error):
    """Drive the module's own `_default_installation_token` to `error`.

    The service is built with an injected factory so the rest of the suite
    stays simple, but the mapping under test lives in the default path — so
    this puts the default path back and makes GitHub's token call raise.
    """
    class _TokenClient:
        def create_installation_access_token(self, installation_id, app_jwt):
            raise error

    service._client = _TokenClient()
    return RepositoryOnboardingService._default_installation_token.__get__(
        service, RepositoryOnboardingService)


class _ObservabilityCase(unittest.TestCase):
    """Shared machinery: run something that fails, capture what was logged."""

    def capture(self, call):
        """Invoke `call`, require the standard refusal, return the log records."""
        with self.assertLogs(LOGGER_NAME, level=logging.WARNING) as captured:
            with self.assertRaises(RepositoryOnboardingError) as raised:
                call()

        error = raised.exception
        # The outward contract, re-asserted on every single path. A caller
        # learns that GitHub did not answer and nothing else — no detail
        # string, and no chained cause that a traceback handler could render
        # into a response.
        self.assertEqual(error.code, CODE_GITHUB_UNAVAILABLE)
        self.assertIsNone(error.detail)
        self.assertIsNone(error.__cause__)

        events = [record for record in captured.records
                  if record.getMessage() == EVENT_GITHUB_UNAVAILABLE]
        self.assertTrue(events, "no structured event was emitted")
        self.assertNoCredentials(captured.records)
        return events

    def assertNoCredentials(self, records):
        """No record, in any field, may carry anything credential-shaped."""
        forbidden = (SENTINEL_TOKEN, SENTINEL_JWT, "Authorization", "Bearer ",
                     "Bad credentials")
        for record in records:
            haystack = [record.getMessage(), str(record.args)]
            haystack.extend(str(value) for value in vars(record).values())
            blob = " ".join(haystack)
            for needle in forbidden:
                self.assertNotIn(
                    needle, blob,
                    f"{needle!r} reached a log record for {record.getMessage()!r}")


class ListingFailures(_ObservabilityCase):
    """GET /installation/repositories — the outermost GitHub call."""

    def test_api_error_is_logged_with_status_and_installation(self):
        client = _FakeClient(list_error=github_error(status_code=503))
        service = build_service(client)

        event, = self.capture(
            lambda: service.list_repositories(_FakeStore(), "tenant-1"))

        self.assertEqual(event.operation, "list_installation_repositories")
        self.assertEqual(event.installation_id, INSTALLATION)
        self.assertEqual(event.http_status, 503)
        self.assertEqual(event.exception_class, "GitHubAPIError")
        # The category, never the message. 503 is an outage to wait out; 403
        # is a permission to go and fix. That is the whole diagnosis.
        self.assertEqual(event.github_message_category, "server")

    def test_the_page_that_failed_is_recorded(self):
        """Page 1 is a dead token or an outage; a later page is a rate limit."""
        client = _FakeClient(list_error=github_error(status_code=429))
        service = build_service(client)

        event, = self.capture(
            lambda: service.list_repositories(_FakeStore(), "tenant-1"))

        self.assertEqual(event.page, 1)
        self.assertEqual(event.github_message_category, "rate_limit")

    def test_a_200_with_the_wrong_shape_is_distinguishable(self):
        """No exception exists to carry a status, so the shape is the finding."""
        client = _FakeClient(list_document=["not", "an", "object"])
        service = build_service(client)

        event, = self.capture(
            lambda: service.list_repositories(_FakeStore(), "tenant-1"))

        self.assertEqual(event.operation, "list_installation_repositories")
        self.assertEqual(event.reason, "response_not_an_object")
        self.assertIsNone(event.exception_class)

    def test_a_missing_repositories_array_is_its_own_reason(self):
        client = _FakeClient(list_document={"total_count": 1})
        service = build_service(client)

        event, = self.capture(
            lambda: service.list_repositories(_FakeStore(), "tenant-1"))

        self.assertEqual(event.reason, "repositories_not_a_list")


class PaginationBoundFailures(_ObservabilityCase):
    """The safety bound that refuses to return a plausible partial list.

    Failing closed here is correct and completely invisible: no exception, no
    status, and a customer who simply cannot list their repositories. What was
    collected against what GitHub advertised is the only thing that separates a
    pagination bug from a genuinely enormous installation.
    """

    def test_exhausting_the_page_bound_is_logged_with_the_counts(self):
        client = _FakeClient(never_completes=True)
        service = build_service(client)

        event, = self.capture(
            lambda: service.list_repositories(_FakeStore(), "tenant-1"))

        self.assertEqual(event.operation, "list_installation_repositories")
        self.assertEqual(event.reason, "page_limit_exhausted")
        self.assertEqual(event.installation_id, INSTALLATION)
        self.assertEqual(event.total_count, 999_999)
        # Deliberately not asserted against the literal bound: the point is
        # that the counts are present and disagree, not what the bound is.
        self.assertGreater(event.collected, 0)
        self.assertLess(event.collected, 999_999)
        self.assertEqual(event.page, event.collected)
        self.assertIsNone(event.exception_class)


class BranchFailures(_ObservabilityCase):
    """The default branch lookup that resolves a head SHA."""

    def test_api_error_names_the_repository_and_branch(self):
        client = _FakeClient(branch_error=github_error(status_code=404))
        service = build_service(client)

        event, = self.capture(
            lambda: service.list_repositories(_FakeStore(), "tenant-1"))

        self.assertEqual(event.operation, "get_branch")
        self.assertEqual(event.owner, OWNER)
        self.assertEqual(event.repository, REPOSITORY)
        self.assertEqual(event.branch, BRANCH)
        self.assertEqual(event.installation_id, INSTALLATION)
        # A 404 here is almost always a stale default branch on record, not an
        # outage — which is exactly the distinction that was missing.
        self.assertEqual(event.github_message_category, "not_found")

    def test_a_branch_response_without_a_commit_sha_is_distinguishable(self):
        client = _FakeClient(branch_document={"name": BRANCH})
        service = build_service(client)

        event, = self.capture(
            lambda: service.list_repositories(_FakeStore(), "tenant-1"))

        self.assertEqual(event.operation, "get_branch")
        self.assertEqual(event.reason, "commit_sha_missing")

    def test_a_non_string_sha_is_distinguishable(self):
        client = _FakeClient(branch_document={"commit": {"sha": None}})
        service = build_service(client)

        event, = self.capture(
            lambda: service.list_repositories(_FakeStore(), "tenant-1"))

        self.assertEqual(event.reason, "commit_sha_not_a_string")


class DbtProbeFailures(_ObservabilityCase):
    """The dbt_project.yml probe, which must never report absence for an outage."""

    def test_api_error_names_the_path_that_was_probed(self):
        client = _FakeClient(file_error=github_error(status_code=403))
        service = build_service(client)

        event, = self.capture(
            lambda: service.detect_dbt_project(ACME_REPOSITORY))

        self.assertEqual(event.operation, "detect_dbt_project")
        self.assertEqual(event.path, "dbt_project.yml")
        self.assertEqual(event.owner, OWNER)
        self.assertEqual(event.repository, REPOSITORY)
        self.assertEqual(event.branch, BRANCH)
        # 403 on the FIRST probe is a contents-permission problem; on a later
        # one it is a rate limit. The path is what separates them.
        self.assertEqual(event.github_message_category, "permission")

    def test_a_missing_file_is_still_not_a_failure(self):
        """Absence is the normal answer and must stay silent."""
        from agent.github_app.client import GitHubNotFoundError

        client = _FakeClient(file_error=GitHubNotFoundError("no such file"))
        service = build_service(client)

        with self.assertNoLogs(LOGGER_NAME, level=logging.WARNING):
            result = service.detect_dbt_project(ACME_REPOSITORY)

        self.assertEqual(result["detected"], False)


class InstallationTokenFailures(_ObservabilityCase):
    """The fail-closed permission check, and everything else token-shaped."""

    def test_permission_drift_is_told_apart_from_an_outage(self):
        """The two need opposite responses: reinstall the App, or wait."""
        from agent.github_app.auth import AuthenticationError

        service = build_service(_FakeClient())
        service._installation_token = _real_token_path(
            service,
            AuthenticationError(
                "GitHub installation token permissions do not match the "
                "approved minimal set: required contents:read."))

        event, = self.capture(
            lambda: service.list_repositories(_FakeStore(), "tenant-1"))

        self.assertEqual(event.operation, "installation_token")
        self.assertEqual(event.installation_id, INSTALLATION)
        self.assertEqual(event.exception_class, "AuthenticationError")
        self.assertEqual(event.reason, "permission_mismatch")

    def test_github_refusing_to_mint_a_token_has_its_own_reason(self):
        from agent.github_app.auth import AuthenticationError

        service = build_service(_FakeClient())
        service._installation_token = _real_token_path(
            service,
            AuthenticationError("GitHub did not return an installation token."))

        event, = self.capture(
            lambda: service.list_repositories(_FakeStore(), "tenant-1"))

        self.assertEqual(event.reason, "token_absent_from_response")

    def test_an_unrecognised_message_is_labelled_never_quoted(self):
        """The label set is closed. An unknown message must not become one."""
        from agent.github_app.auth import AuthenticationError

        service = build_service(_FakeClient())
        service._installation_token = _real_token_path(
            service,
            AuthenticationError(f"something new about {SENTINEL_TOKEN}"))

        event, = self.capture(
            lambda: service.list_repositories(_FakeStore(), "tenant-1"))

        self.assertEqual(event.reason, "unclassified")


class NothingSensitiveIsEverLogged(_ObservabilityCase):
    """The rule stated once, over every instrumented path at once."""

    def test_no_path_leaks_the_error_message_github_sent(self):
        from agent.github_app.auth import AuthenticationError

        listing = build_service(_FakeClient(list_error=github_error(status_code=401)))
        branch = build_service(_FakeClient(branch_error=github_error(status_code=401)))
        probe = build_service(_FakeClient(file_error=github_error(status_code=401)))
        token = build_service(_FakeClient())
        token._installation_token = _real_token_path(
            token, AuthenticationError(f"presented {SENTINEL_TOKEN}"))

        store = _FakeStore()
        # `capture` asserts the absence on every record it sees, so running all
        # four through it is the assertion.
        self.capture(lambda: listing.list_repositories(store, "tenant-1"))
        self.capture(lambda: branch.list_repositories(store, "tenant-1"))
        self.capture(lambda: probe.detect_dbt_project(ACME_REPOSITORY))
        self.capture(lambda: token.list_repositories(store, "tenant-1"))

    def test_the_installation_token_is_not_an_argument_the_logger_can_see(self):
        """Belt and braces: the token never enters the helper's signature."""
        from agent.api import repository_onboarding

        source = pathlib.Path(repository_onboarding.__file__).read_text(
            encoding="utf-8")
        tree = ast.parse(source)

        banned = {"token", "app_jwt", "jwt", "secret", "authorization",
                  "credential", "presented"}
        offenders = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_log_github_unavailable"):
                continue
            for keyword in node.keywords:
                if keyword.arg and keyword.arg.lower() in banned:
                    offenders.append((node.lineno, keyword.arg))
                value = keyword.value
                if isinstance(value, ast.Name) and value.id.lower() in banned:
                    offenders.append((node.lineno, value.id))
        self.assertEqual(offenders, [])


class OneRepositoryWithNoResolvableBranch(unittest.TestCase):
    """The production fault: one odd repository took the whole list down.

    A customer authorized `youtube-skeleton-clone`, which is empty. An empty
    repository has no commits, so its default branch does not exist and
    GitHub answers 404 for it. That 404 was mapped to `github_unavailable`
    like any other provider error, and GET /api/onboarding/repositories
    returned a conflict — so a customer with one unused repository could not
    see or connect any of the others.

    The fix degrades that ONE repository. These tests hold the line on both
    sides of it: the list survives a 404, and it still refuses everything
    else. The second half matters more than the first — `except
    GitHubAPIError` here instead of `except GitHubNotFoundError` would make a
    revoked installation look like an empty repository, which is a far worse
    bug than the one being fixed.
    """

    def setUp(self):
        from agent.github_app.client import GitHubNotFoundError

        self.not_found = GitHubNotFoundError(
            "Branch not found", status_code=404,
            operation="get_repository_branch", http_method="GET",
            route_template="/repos/{owner}/{repo}/branches/{branch}")
        self.client = _FakeClient(
            list_document=TWO_REPOSITORIES,
            branch_errors={EMPTY_REPOSITORY: self.not_found})
        self.service = build_service(self.client)

    def listed(self, store=None):
        return self.service.list_repositories(store or _FakeStore(), "tenant-1")

    # -- the list survives -----------------------------------------------

    def test_a_404_on_one_repository_does_not_collapse_the_list(self):
        repositories = self.listed()

        self.assertEqual(
            sorted(r.name for r in repositories),
            sorted([REPOSITORY, EMPTY_REPOSITORY]))

    def test_the_healthy_repository_still_resolves_its_commit(self):
        """Degrading the neighbour must not degrade the ones that answered."""
        healthy, = [r for r in self.listed() if r.name == REPOSITORY]

        self.assertEqual(healthy.head_sha, "a" * 40)
        self.assertFalse(healthy.branch_unresolved)

    def test_the_problematic_repository_stays_visible_and_is_marked(self):
        empty, = [r for r in self.listed() if r.name == EMPTY_REPOSITORY]

        # Visible, because the customer is authorized for it and may well want
        # to connect it once they push a first commit.
        self.assertEqual(empty.full_name, f"{OWNER}/{EMPTY_REPOSITORY}")
        self.assertIsNone(empty.head_sha)
        self.assertTrue(empty.branch_unresolved)

    def test_every_repository_being_empty_is_still_a_list_not_an_error(self):
        """The degenerate case: a brand-new account with nothing pushed yet."""
        client = _FakeClient(
            list_document=TWO_REPOSITORIES,
            branch_errors={REPOSITORY: self.not_found,
                           EMPTY_REPOSITORY: self.not_found})

        repositories = build_service(client).list_repositories(
            _FakeStore(), "tenant-1")

        self.assertEqual(len(repositories), 2)
        self.assertTrue(all(r.branch_unresolved for r in repositories))

    # -- the dbt probe is not attempted ----------------------------------

    def test_dbt_detection_is_skipped_for_the_unresolved_repository(self):
        """No commit means no ref; probing would be five guaranteed 404s."""
        self.listed()

        probed = {name for name, _path in self.client.get_file_calls}
        self.assertEqual(probed, {REPOSITORY})

    def test_detection_is_skipped_when_called_directly_too(self):
        """select_repository reaches the probe by another road."""
        unresolved = dataclass_replace_for_test(
            ACME_REPOSITORY, branch_unresolved=True)

        result = self.service.detect_dbt_project(unresolved)

        self.assertEqual(
            result,
            {"detected": False, "project_dir": None, "manifest_path": None})
        self.assertEqual(self.client.get_file_calls, [])

    def test_a_stored_detection_is_not_overwritten_by_the_degraded_pass(self):
        """The load-bearing half of skipping the upsert.

        This repository was detected as a dbt project yesterday, when it had
        commits. A transient branch 404 today must not rewrite that to "no dbt
        project" — the customer would be told their configured repository is
        not a dbt project at all.
        """
        store = _RecordingStore(detections=[{
            "github_repository_id": 900002, "dbt_detected": True,
            "dbt_project_dir": "analytics", "default_branch": BRANCH,
            "dbt_checked_commit_sha": "b" * 40,
        }])

        self.listed(store)

        written = {call["github_repository_id"] for call in store.upserts}
        self.assertNotIn(900002, written)
        self.assertEqual(written, {900001})

    # -- and everything else still fails closed ---------------------------

    def _assert_fails_closed(self, error):
        client = _FakeClient(list_document=TWO_REPOSITORIES,
                             branch_errors={EMPTY_REPOSITORY: error})
        service = build_service(client)

        with self.assertLogs(LOGGER_NAME, level=logging.WARNING) as captured:
            with self.assertRaises(RepositoryOnboardingError) as raised:
                service.list_repositories(_FakeStore(), "tenant-1")

        self.assertEqual(raised.exception.code, CODE_GITHUB_UNAVAILABLE)
        self.assertIn(EVENT_GITHUB_UNAVAILABLE,
                      [r.getMessage() for r in captured.records])

    def test_a_401_on_one_repository_still_fails_the_whole_list(self):
        """A dead installation token is not an empty repository."""
        self._assert_fails_closed(github_error(status_code=401))

    def test_a_403_on_one_repository_still_fails_the_whole_list(self):
        """Nor is a revoked permission. THIS is the one that must not degrade."""
        self._assert_fails_closed(github_error(status_code=403))

    def test_a_429_on_one_repository_still_fails_the_whole_list(self):
        self._assert_fails_closed(github_error(status_code=429))

    def test_a_500_on_one_repository_still_fails_the_whole_list(self):
        self._assert_fails_closed(github_error(status_code=503))

    def test_a_malformed_branch_document_still_fails_closed(self):
        """Only a 404 degrades. A 200 that makes no sense is still an outage."""
        client = _FakeClient(list_document=TWO_REPOSITORIES,
                             branch_document={"commit": {}})
        service = build_service(client)

        with self.assertLogs(LOGGER_NAME, level=logging.WARNING):
            with self.assertRaises(RepositoryOnboardingError) as raised:
                service.list_repositories(_FakeStore(), "tenant-1")

        self.assertEqual(raised.exception.code, CODE_GITHUB_UNAVAILABLE)

    # -- what it logs, and what it must not ------------------------------

    def test_the_handled_404_gets_its_own_event_with_the_repository_named(self):
        with self.assertLogs(LOGGER_NAME, level=logging.INFO) as captured:
            self.listed()

        event, = [r for r in captured.records
                  if r.getMessage() == EVENT_BRANCH_UNRESOLVED]
        self.assertEqual(event.operation, "get_branch")
        self.assertEqual(event.owner, OWNER)
        self.assertEqual(event.repository, EMPTY_REPOSITORY)
        self.assertEqual(event.branch, BRANCH)
        self.assertEqual(event.installation_id, INSTALLATION)
        self.assertEqual(event.http_status, 404)
        self.assertEqual(event.degraded, "branch_unresolved")

    def test_a_handled_404_never_emits_the_fail_closed_event(self):
        """The whole point of a separate name.

        `onboarding_github_unavailable` means the request failed. Emitting it
        for something that changed nothing about the response would make the
        alert it feeds permanently untrustworthy — and would have hidden this
        very bug, since the production line that led here looks identical
        either way.
        """
        with self.assertLogs(LOGGER_NAME, level=logging.INFO) as captured:
            self.listed()

        self.assertNotIn(EVENT_GITHUB_UNAVAILABLE,
                         [r.getMessage() for r in captured.records])

    def test_the_degraded_path_logs_at_info_not_warning(self):
        """An empty repository is ordinary. It must not page anybody."""
        with self.assertNoLogs(LOGGER_NAME, level=logging.WARNING):
            self.listed()

    def test_the_degraded_path_leaks_nothing_either(self):
        from agent.github_app.client import GitHubNotFoundError

        client = _FakeClient(
            list_document=TWO_REPOSITORIES,
            branch_errors={EMPTY_REPOSITORY: GitHubNotFoundError(
                f"Not Found for token {SENTINEL_TOKEN}", status_code=404)})

        with self.assertLogs(LOGGER_NAME, level=logging.INFO) as captured:
            build_service(client).list_repositories(_FakeStore(), "tenant-1")

        blob = " ".join(str(value) for record in captured.records
                        for value in vars(record).values())
        self.assertNotIn(SENTINEL_TOKEN, blob)
        self.assertNotIn(SENTINEL_JWT, blob)


def dataclass_replace_for_test(repository, **changes):
    from dataclasses import replace

    return replace(repository, **changes)


class EveryMappingSiteIsInstrumented(unittest.TestCase):
    """The net under the whole patch.

    A tenth place that maps a provider failure to ``github_unavailable`` will
    be added one day, and it will be added by somebody who has never read this
    file. This fails that change rather than letting it reopen the blind spot
    that started all of it.
    """

    def test_no_github_unavailable_raise_is_silent(self):
        from agent.api import repository_onboarding

        source = pathlib.Path(repository_onboarding.__file__).read_text(
            encoding="utf-8")
        tree = ast.parse(source)

        def is_unavailable_raise(node):
            if not (isinstance(node, ast.Raise)
                    and isinstance(node.exc, ast.Call)):
                return False
            func = node.exc.func
            if not (isinstance(func, ast.Name)
                    and func.id == "RepositoryOnboardingError"):
                return False
            return any(isinstance(arg, ast.Name)
                       and arg.id == "CODE_GITHUB_UNAVAILABLE"
                       for arg in node.exc.args)

        def is_log(node):
            return (isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == "_log_github_unavailable")

        # `orelse` is in the list because one of the sites is a `for ... else`
        # — the pagination safety bound. A walker that only looked at `body`
        # and exception handlers would miss it and pass vacuously.
        blocks = []
        for parent in ast.walk(tree):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(parent, field, None)
                if isinstance(block, list):
                    blocks.append(block)
            for handler in getattr(parent, "handlers", None) or []:
                blocks.append(handler.body)

        silent, instrumented = [], []
        for block in blocks:
            for index, node in enumerate(block):
                if not is_unavailable_raise(node):
                    continue
                if index > 0 and is_log(block[index - 1]):
                    instrumented.append(node.lineno)
                else:
                    silent.append(node.lineno)

        self.assertEqual(sorted(set(silent)), [],
                         "these raises discard the reason without logging it")
        # A guard that guards nothing is worse than none, so the count is
        # asserted too: if the raises are refactored away, this must be
        # revisited rather than passing vacuously.
        self.assertGreaterEqual(len(set(instrumented)), 9)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
