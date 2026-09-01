"""Free / Starter / Pro: what each plan includes, and where that is enforced.

###################################################################
# THE ASSERTIONS THAT MATTER MOST ARE THE ONES ABOUT FREE.        #
###################################################################

Two rules run through this file:

  1. A paid capability is refused SERVER-SIDE, from Relium's own billing row,
     for a caller holding a perfectly valid token. The dashboard hiding a
     button is not what stops anyone.

  2. Core analysis is never metered. Free gets the same SQL/dbt analysis, the
     same blast radius, the same semantic detection and the same
     ALLOW/WARN/BLOCK decision. What Free does not get is paid EVIDENCE,
     scale, retention and enforcement.

The second is the easier one to break by accident, so it is asserted directly:
the dbt manifest endpoint must never acquire a plan gate, and a Free repository
whose relium.yml says `enforce` still gets a full review and a real BLOCK
decision — reported, not enforced.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from agent.billing.entitlements import (
    CAPABILITIES, FREE, MERGE_BLOCKING, PRO, RUNTIME_EVIDENCE, STARTER,
    UNMETERED, WAREHOUSE_EVIDENCE, entitlements_for, plan_including)
from agent.billing.plans import PLAN_FREE, PLAN_PRO, PLAN_STARTER

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


class FakeStore:
    """Just enough store for the entitlement resolvers."""

    def __init__(self, billing=None, slugs=None):
        self.billing = billing or {}
        self.slugs = slugs or {}

    def billing_for_tenant(self, tenant_id):
        return self.billing.get(tenant_id)

    def tenant_for_repository_slug(self, organization_id, repository_id):
        return self.slugs.get((organization_id, repository_id))


class Settings:
    """Stands in for PolarSettings. Only the grace period is read."""

    past_due_grace = timedelta(0)


class Scope:
    def __init__(self, organization_id, repository_id):
        self.organization_id = organization_id
        self.repository_id = repository_id


# ---------------------------------------------------------------- the catalog

class PlanCatalogTests(unittest.TestCase):

    def test_free_entitlements(self):
        e = entitlements_for(PLAN_FREE)
        self.assertEqual(e.repository_limit, 1)
        self.assertEqual(e.member_limit, 2)
        self.assertEqual(e.history_retention_days, 7)
        self.assertFalse(e.warehouse_evidence)
        self.assertFalse(e.runtime_evidence)
        self.assertFalse(e.custom_review_policies)
        self.assertFalse(e.merge_blocking)
        self.assertFalse(e.governance_controls)

    def test_starter_entitlements(self):
        e = entitlements_for(PLAN_STARTER)
        self.assertEqual(e.repository_limit, 3)
        self.assertEqual(e.member_limit, 10)
        self.assertEqual(e.history_retention_days, 90)
        self.assertTrue(e.warehouse_evidence)
        self.assertTrue(e.runtime_evidence)
        # Starter uses the standard production policy set, not its own.
        self.assertFalse(e.custom_review_policies)
        self.assertFalse(e.merge_blocking)
        self.assertFalse(e.governance_controls)

    def test_pro_entitlements(self):
        e = entitlements_for(PLAN_PRO)
        self.assertIsNone(e.repository_limit)
        self.assertIsNone(e.member_limit)
        self.assertIsNone(e.history_retention_days)
        for capability in ("warehouse_evidence", "runtime_evidence",
                           "custom_review_policies", "merge_blocking",
                           "governance_controls"):
            self.assertTrue(getattr(e, capability), capability)

    def test_an_unknown_plan_fails_closed_to_free(self):
        for plan in ("enterprise", "PRO", "", None, 0, "starter ", "trial"):
            self.assertEqual(entitlements_for(plan), FREE, plan)

    def test_unlimited_is_none_and_never_a_large_number(self):
        """A sentinel that reads as a number is an off-by-one waiting to
        happen at the boundary."""
        self.assertIsNone(PRO.repository_limit)
        self.assertFalse(PRO.exceeds_repositories(10_000))
        self.assertTrue(PRO.unlimited_history)

    def test_limits_are_not_readable_as_boolean_capabilities(self):
        """`allows("repository_limit")` would be true for every plan, including
        one with a limit of zero. It refuses instead of lying."""
        with self.assertRaises(ValueError):
            FREE.allows("repository_limit")
        with self.assertRaises(ValueError):
            PRO.allows("history_retention_days")

    def test_repository_limit_boundaries(self):
        self.assertFalse(FREE.exceeds_repositories(1))
        self.assertTrue(FREE.exceeds_repositories(2))
        self.assertFalse(STARTER.exceeds_repositories(3))
        self.assertTrue(STARTER.exceeds_repositories(4))

    def test_member_limit_boundaries(self):
        self.assertFalse(FREE.exceeds_members(2))
        self.assertTrue(FREE.exceeds_members(3))
        self.assertFalse(STARTER.exceeds_members(10))
        self.assertTrue(STARTER.exceeds_members(11))
        self.assertFalse(PRO.exceeds_members(1000))

    def test_the_payload_is_capabilities_and_nothing_else(self):
        payload = STARTER.as_payload()
        self.assertEqual(set(payload), set(CAPABILITIES))
        for value in payload.values():
            self.assertIsInstance(value, (bool, int, type(None)))

    def test_plan_including_names_the_weakest_plan_that_unlocks_a_capability(self):
        self.assertEqual(plan_including(WAREHOUSE_EVIDENCE), PLAN_STARTER)
        self.assertEqual(plan_including(RUNTIME_EVIDENCE), PLAN_STARTER)
        self.assertEqual(plan_including(MERGE_BLOCKING), PLAN_PRO)
        self.assertEqual(plan_including("custom_review_policies"), PLAN_PRO)
        self.assertEqual(plan_including("governance_controls"), PLAN_PRO)

    def test_paid_plans_never_reduce_a_capability(self):
        """Nothing is taken away by paying. A regression here would mean the
        catalog had grown a capability that gets WORSE on a higher plan."""
        for capability in CAPABILITIES:
            free, starter, pro = (getattr(p, capability)
                                  for p in (FREE, STARTER, PRO))
            if isinstance(free, bool):
                self.assertLessEqual(int(free), int(starter), capability)
                self.assertLessEqual(int(starter), int(pro), capability)
            else:
                # None is unlimited, so it sorts above every number.
                rank = lambda v: float("inf") if v is None else v  # noqa: E731
                self.assertLessEqual(rank(free), rank(starter), capability)
                self.assertLessEqual(rank(starter), rank(pro), capability)


# ------------------------------------------------------- subscription -> plan

class WorkspaceEntitlementTests(unittest.TestCase):
    """The row is history; the entitlement is what is owed right now."""

    def _entitlements(self, record, settings=Settings()):
        from agent.billing.access import get_workspace_entitlements

        store = FakeStore({"t1": record} if record else {})
        return get_workspace_entitlements(store, "t1", settings, now=NOW)

    def test_a_workspace_that_never_bought_is_free(self):
        self.assertEqual(self._entitlements(None), FREE)

    def test_an_active_starter_subscription_grants_starter(self):
        self.assertEqual(self._entitlements(
            {"plan": "starter", "subscription_status": "active"}), STARTER)

    def test_an_active_pro_subscription_grants_pro(self):
        self.assertEqual(self._entitlements(
            {"plan": "pro", "subscription_status": "active"}), PRO)

    def test_a_trialing_subscription_is_entitled(self):
        self.assertEqual(self._entitlements(
            {"plan": "pro", "subscription_status": "trialing"}), PRO)

    def test_an_inactive_subscription_falls_back_to_free(self):
        for status in ("incomplete", "incomplete_expired", "unpaid", "paused"):
            self.assertEqual(self._entitlements(
                {"plan": "pro", "subscription_status": status}), FREE, status)

    def test_a_canceled_or_revoked_subscription_is_free(self):
        """The row still says pro. The entitlement does not."""
        record = {"plan": "pro", "subscription_status": "canceled"}
        self.assertEqual(self._entitlements(record), FREE)

    def test_a_scheduled_cancellation_keeps_its_plan_until_the_period_ends(self):
        """Polar keeps a cancelling subscription `active`; the customer paid
        for the period. Only the revocation ends it."""
        self.assertEqual(self._entitlements({
            "plan": "pro", "subscription_status": "active",
            "cancel_at_period_end": True}), PRO)

    def test_past_due_without_grace_is_free(self):
        self.assertEqual(self._entitlements({
            "plan": "pro", "subscription_status": "past_due",
            "past_due_at": NOW - timedelta(hours=1)}), FREE)

    def test_an_unknown_status_grants_nothing(self):
        self.assertEqual(self._entitlements({
            "plan": "pro", "subscription_status": "gift_subscription"}), FREE)

    def test_an_unknown_plan_in_the_row_fails_closed(self):
        self.assertEqual(self._entitlements({
            "plan": "enterprise", "subscription_status": "active"}), FREE)

    def test_a_deployment_without_polar_meters_nothing(self):
        """No Polar configuration means nobody can upgrade, so metering would
        pin a self-hosted install to Free forever."""
        self.assertEqual(self._entitlements(None, settings=None), UNMETERED)
        self.assertTrue(UNMETERED.warehouse_evidence)
        self.assertIsNone(UNMETERED.repository_limit)


class ScopeResolutionTests(unittest.TestCase):
    """Service tokens are scoped by owner/name; billing is keyed by tenant."""

    def test_a_repository_resolves_to_its_workspace_entitlements(self):
        from agent.billing.access import entitlements_for_scope

        store = FakeStore(
            billing={"t1": {"plan": "starter", "subscription_status": "active"}},
            slugs={("acme", "warehouse-dbt"): "t1"})
        self.assertEqual(
            entitlements_for_scope(store, Scope("acme", "warehouse-dbt"),
                                   Settings(), now=NOW),
            STARTER)

    def test_a_repository_no_workspace_owns_is_not_metered(self):
        """A deployment that predates Clerk tenancy has repositories with no
        workspace. Refusing their evidence would break an install that worked
        before entitlements existed, and there is no subscription to enforce."""
        from agent.billing.access import entitlements_for_scope

        store = FakeStore(slugs={})
        self.assertEqual(
            entitlements_for_scope(store, Scope("acme", "orphan"), Settings()),
            UNMETERED)

    def test_one_tenants_plan_never_answers_for_another(self):
        from agent.billing.access import entitlements_for_scope

        store = FakeStore(
            billing={"paid": {"plan": "pro", "subscription_status": "active"},
                     "unpaid": {}},
            slugs={("acme", "paid-repo"): "paid",
                   ("acme", "free-repo"): "unpaid"})
        self.assertEqual(entitlements_for_scope(
            store, Scope("acme", "paid-repo"), Settings(), now=NOW), PRO)
        self.assertEqual(entitlements_for_scope(
            store, Scope("acme", "free-repo"), Settings(), now=NOW), FREE)


# ------------------------------------------------------- the billing response

class BillingResponseTests(unittest.TestCase):

    def _view(self, record):
        from agent.billing.service import BillingService

        service = BillingService.__new__(BillingService)
        service._settings = Settings()
        service._clock = lambda: NOW
        return service.view_for_record(record)

    def test_a_free_workspace_is_told_frees_entitlements(self):
        view = self._view(None)
        self.assertEqual(view["plan"], "free")
        self.assertEqual(view["entitlements"], FREE.as_payload())

    def test_entitlements_follow_the_effective_plan_not_the_stored_one(self):
        """A revoked Pro row reports Free's capabilities, because that is what
        the workspace has."""
        view = self._view({"plan": "pro", "subscription_status": "canceled",
                           "polar_customer_id": "cus_x"})
        self.assertEqual(view["plan"], "free")
        self.assertEqual(view["entitlements"], FREE.as_payload())
        self.assertTrue(view["has_billing_account"])

    def test_an_active_pro_row_reports_pro_entitlements(self):
        view = self._view({"plan": "pro", "subscription_status": "active"})
        self.assertEqual(view["entitlements"], PRO.as_payload())

    def test_the_entitlement_payload_carries_no_polar_identifier(self):
        import json

        view = self._view({
            "plan": "starter", "subscription_status": "active",
            "polar_customer_id": "cus_secret",
            "polar_subscription_id": "sub_secret",
            "polar_product_id": "prod_secret"})
        text = json.dumps(view["entitlements"])
        for identifier in ("cus_secret", "sub_secret", "prod_secret"):
            self.assertNotIn(identifier, text)


# --------------------------------------------------------- repository limits

class RepositoryLimitTests(unittest.TestCase):
    """The limit is enforced in the store, inside the inserting transaction.

    These exercise the decision, not PostgreSQL. The transactional behavior --
    the tenant-row lock that makes two concurrent selections serialise -- is
    covered by the PostgreSQL suite in test_onboarding_postgres.py.
    """

    def test_free_allows_the_first_repository_and_refuses_the_second(self):
        self.assertFalse(FREE.exceeds_repositories(0 + 1))
        self.assertTrue(FREE.exceeds_repositories(1 + 1))

    def test_starter_allows_three(self):
        for connected in (0, 1, 2):
            self.assertFalse(STARTER.exceeds_repositories(connected + 1), connected)
        self.assertTrue(STARTER.exceeds_repositories(3 + 1))

    def test_pro_is_unlimited(self):
        for connected in (0, 3, 50, 5000):
            self.assertFalse(PRO.exceeds_repositories(connected + 1), connected)

    def test_a_downgraded_workspace_keeps_what_it_has(self):
        """Pro -> Free with four repositories. Nothing is deleted, and the
        limit only refuses a FIFTH."""
        connected = 4
        self.assertTrue(FREE.exceeds_repositories(connected + 1))
        # ...but re-selecting one of the four is not a new connection, which is
        # why the store checks membership before it counts.
        self.assertEqual(FREE.repository_limit, 1)


class RepositoryLimitStoreTests(unittest.TestCase):
    """The store's own branch logic, against a recording fake connection."""

    class FakeConnection:
        def __init__(self, existing, tenant_rows):
            self.existing = existing
            self.tenant_rows = tenant_rows
            self.statements = []

        def transaction(self):
            import contextlib

            return contextlib.nullcontext()

        def execute(self, sql, params=None):
            self.statements.append(sql)
            outer = self

            class Result:
                def fetchone(self):
                    if "FOR UPDATE" in sql:
                        return {"tenant_id": params[0]}
                    if "WHERE tenant_id = %s AND github_repository_id = %s" in sql:
                        return {"x": 1} if params[1] in outer.existing else None
                    if "count(*)" in sql:
                        return {"n": len(outer.existing)}
                    if "INSERT INTO tenant_repositories" in sql:
                        outer.existing.add(params[0])
                        return {"github_repository_id": params[0],
                                "tenant_id": params[1]}
                    return None

                def fetchall(self):
                    return outer.tenant_rows

            return Result()

    def _store(self, existing=()):
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        store = PostgresLifecycleStore.__new__(PostgresLifecycleStore)
        store.connection = self.FakeConnection(set(existing), [])
        return store

    def _select(self, store, repository_id, limit):
        return store.select_tenant_repository(
            repository_id, tenant_id="t1", github_installation_id=1,
            owner_login="acme", name=f"repo{repository_id}",
            repository_limit=limit)

    def test_the_first_repository_is_allowed_on_free(self):
        store = self._store()
        self.assertIsNotNone(self._select(store, 100, 1))

    def test_the_second_repository_is_refused_on_free(self):
        from agent.postgres_lifecycle_store import TenantRepositoryLimitReached

        store = self._store(existing={100})
        with self.assertRaises(TenantRepositoryLimitReached):
            self._select(store, 200, 1)

    def test_reselecting_a_repository_already_connected_is_never_refused(self):
        """The idempotent re-select. A workspace over its allowance after a
        downgrade must still be able to step back and forward through
        onboarding on the repositories it already has."""
        store = self._store(existing={100, 200, 300, 400})
        self.assertIsNotNone(self._select(store, 300, 1))

    def test_an_unlimited_plan_takes_no_lock_at_all(self):
        store = self._store(existing={1, 2, 3})
        self._select(store, 400, None)
        self.assertFalse(any("FOR UPDATE" in s for s in store.connection.statements))

    def test_a_metered_plan_locks_the_tenant_row_before_counting(self):
        """Without the lock two concurrent selections both read the same count
        and both insert."""
        store = self._store()
        self._select(store, 100, 3)
        statements = store.connection.statements
        lock = next(i for i, s in enumerate(statements) if "FOR UPDATE" in s)
        count = next(i for i, s in enumerate(statements) if "count(*)" in s)
        insert = next(i for i, s in enumerate(statements)
                      if "INSERT INTO tenant_repositories" in s)
        self.assertLess(lock, count)
        self.assertLess(count, insert)


# --------------------------------------------------- merge blocking (the gate)

class MergeBlockingTests(unittest.TestCase):
    """Enforcement is Pro. The DECISION is everyone's.

    `enforcement_mode` is read from relium.yml in the customer's own
    repository, so this -- not the dashboard -- is the boundary.
    """

    def _runner(self, allowed):
        from agent.github_app.runner import PullRequestReviewRunner

        runner = PullRequestReviewRunner.__new__(PullRequestReviewRunner)
        runner._merge_blocking_allowed = allowed
        return runner

    def _config(self, enforcement_mode):
        from agent.github_app.config import load_repository_config

        return load_repository_config(
            f"enabled: true\nenforcement_mode: {enforcement_mode}\n".encode())

    def test_a_free_repository_asking_to_enforce_is_downgraded_to_shadow(self):
        runner = self._runner(lambda owner, repo: False)
        capped = runner._apply_entitlements(
            self._config("enforce"), "acme", "dbt")
        self.assertEqual(capped.enforcement_mode, "shadow")

    def test_a_pro_repository_keeps_enforce(self):
        runner = self._runner(lambda owner, repo: True)
        capped = runner._apply_entitlements(
            self._config("enforce"), "acme", "dbt")
        self.assertEqual(capped.enforcement_mode, "enforce")

    def test_shadow_is_never_escalated(self):
        runner = self._runner(lambda owner, repo: True)
        capped = runner._apply_entitlements(
            self._config("shadow"), "acme", "dbt")
        self.assertEqual(capped.enforcement_mode, "shadow")

    def test_a_deployment_without_polar_honours_the_repository_file(self):
        runner = self._runner(None)
        capped = runner._apply_entitlements(
            self._config("enforce"), "acme", "dbt")
        self.assertEqual(capped.enforcement_mode, "enforce")

    def test_a_failing_billing_lookup_withholds_the_gate_rather_than_granting_it(self):
        def explode(owner, repository):
            raise RuntimeError("database is down")

        capped = self._runner(explode)._apply_entitlements(
            self._config("enforce"), "acme", "dbt")
        self.assertEqual(capped.enforcement_mode, "shadow")

    def test_nothing_but_the_enforcement_mode_changes(self):
        """The cap must not touch the manifest path, the evidence policy or
        anything else that decides what the review actually does."""
        import dataclasses

        original = self._config("enforce")
        capped = self._runner(lambda o, r: False)._apply_entitlements(
            original, "acme", "dbt")
        before = dataclasses.asdict(original)
        after = dataclasses.asdict(capped)
        before.pop("enforcement_mode")
        after.pop("enforcement_mode")
        self.assertEqual(before, after)

    def test_the_check_conclusion_is_the_only_thing_enforcement_changes(self):
        """A BLOCK is still a BLOCK on Free. It is reported as neutral instead
        of failure -- analysis and recommendation, without the gate."""
        from agent.github_app.checks import conclusion_for_decision

        self.assertEqual(
            conclusion_for_decision("BLOCK", enforcement_mode="shadow"),
            "neutral")
        self.assertEqual(
            conclusion_for_decision("BLOCK", enforcement_mode="enforce"),
            "failure")


# ------------------------------------------------- core analysis is not metered

class CoreAnalysisIsNeverMeteredTests(unittest.TestCase):
    """The rule this whole feature must not break."""

    def test_the_dbt_manifest_endpoint_has_no_plan_gate(self):
        """The manifest is the INPUT to the analysis Free includes. Metering it
        would not withhold a paid feature, it would make the free product's own
        answers worse."""
        from agent.api.routes import _COLLECTOR_PLAN_CAPABILITY

        self.assertNotIn("submit_manifest_evidence", _COLLECTOR_PLAN_CAPABILITY)

    def test_only_the_warehouse_snapshot_is_gated_on_the_collector_surface(self):
        from agent.api.routes import _COLLECTOR_PLAN_CAPABILITY

        self.assertEqual(_COLLECTOR_PLAN_CAPABILITY,
                         {"submit_snapshot": WAREHOUSE_EVIDENCE})

    def test_the_collection_request_lifecycle_is_never_gated(self):
        """Refusing an acknowledgement strands a collector retrying a request
        it can never settle."""
        from agent.api.routes import _COLLECTOR_PLAN_CAPABILITY

        for name in ("acknowledge_collection_request",
                     "report_collection_failure", "register_collector",
                     "list_collection_requests"):
            self.assertNotIn(name, _COLLECTOR_PLAN_CAPABILITY, name)

    def test_no_capability_in_the_catalog_degrades_analysis(self):
        """A guard on the catalog itself. Every capability names evidence,
        scale, retention, automation, enforcement or governance -- never the
        correctness of a decision."""
        forbidden = ("semantic", "lineage", "blast", "decision", "accuracy",
                     "detection", "downstream", "schema_breaking")
        for capability in CAPABILITIES:
            for word in forbidden:
                self.assertNotIn(word, capability)


# ------------------------------------------------- the API refuses, not the UI

class TokenPrincipal:
    """A service-token principal, exactly as the authenticator produces one."""

    is_human = False
    identity_provider = None
    environment = None

    def __init__(self, organization_id, repository_id, scope="collector"):
        self.organization_id = organization_id
        self.repository_id = repository_id
        self.scope = scope
        self.token_id = "tok_1"


class StubPool:
    def __init__(self, store):
        self._store = store

    def acquire(self):
        import contextlib

        return contextlib.nullcontext(self._store)


class PaidEvidenceApiTests(unittest.TestCase):
    """A Free workspace holding a VALID collector token still cannot ingest
    warehouse or runtime evidence.

    This is the assertion that matters: the dashboard hiding a section is UX,
    and a collector speaks to the API directly with a credential Relium itself
    issued. The refusal has to happen here.
    """

    def _client(self, plan, *, settings=Settings()):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        from agent.api.routes import create_api_routes

        billing = ({"t1": {"plan": plan, "subscription_status": "active"}}
                   if plan else {})
        store = FakeStore(billing=billing,
                          slugs={("acme", "warehouse-dbt"): "t1"})

        class Authenticator:
            def __init__(self, _store):
                pass

            def authenticate(self, token):
                return TokenPrincipal("acme", "warehouse-dbt")

        routes = create_api_routes(
            store_pool=StubPool(store),
            authenticator_factory=Authenticator,
            billing_settings=settings)
        return TestClient(Starlette(routes=routes))

    PAID = (
        ("/api/metadata-snapshots", "warehouse_evidence", "starter"),
        ("/api/monitoring/observations", "runtime_evidence", "starter"),
        ("/api/monitoring/baselines", "runtime_evidence", "starter"),
        ("/api/anomalies", "runtime_evidence", "starter"),
    )

    def test_free_is_refused_paid_evidence_ingestion(self):
        client = self._client("free")
        for path, capability, required in self.PAID:
            response = client.post(path, json={}, headers={
                "Authorization": "Bearer rlm_free.secret"})
            self.assertEqual(response.status_code, 402, path)
            body = response.json()
            self.assertEqual(body["code"], "plan_upgrade_required", path)
            self.assertEqual(body["capability"], capability, path)
            self.assertEqual(body["required_plan"], required, path)

    def test_a_workspace_that_never_bought_is_refused_the_same_way(self):
        client = self._client(None)
        response = client.post("/api/metadata-snapshots", json={}, headers={
            "Authorization": "Bearer rlm_free.secret"})
        self.assertEqual(response.status_code, 402)

    def test_starter_is_not_refused(self):
        """Past the plan gate. What the handler then does with an empty body is
        the collector suite's business, not this one's -- the assertion here is
        that entitlements stopped standing in the way."""
        client = self._client("starter")
        for path, _, _ in self.PAID:
            response = client.post(path, json={}, headers={
                "Authorization": "Bearer rlm_starter.secret"})
            self.assertNotEqual(response.status_code, 402, path)

    def test_pro_is_not_refused(self):
        client = self._client("pro")
        for path, _, _ in self.PAID:
            response = client.post(path, json={}, headers={
                "Authorization": "Bearer rlm_pro.secret"})
            self.assertNotEqual(response.status_code, 402, path)

    def test_a_revoked_pro_subscription_is_refused(self):
        """Downgrade by revocation. The row still says pro."""
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        from agent.api.routes import create_api_routes

        store = FakeStore(
            billing={"t1": {"plan": "pro", "subscription_status": "canceled"}},
            slugs={("acme", "warehouse-dbt"): "t1"})

        class Authenticator:
            def __init__(self, _store):
                pass

            def authenticate(self, token):
                return TokenPrincipal("acme", "warehouse-dbt")

        client = TestClient(Starlette(routes=create_api_routes(
            store_pool=StubPool(store), authenticator_factory=Authenticator,
            billing_settings=Settings())))
        response = client.post("/api/metadata-snapshots", json={}, headers={
            "Authorization": "Bearer rlm.secret"})
        self.assertEqual(response.status_code, 402)

    def test_the_dbt_manifest_endpoint_is_never_refused_on_free(self):
        """Core analysis. A 402 here would make Free's own PR analysis worse,
        which is the one thing entitlements may not do."""
        client = self._client("free")
        response = client.post("/api/manifest-evidence", json={}, headers={
            "Authorization": "Bearer rlm_free.secret"})
        self.assertNotEqual(response.status_code, 402)

    def test_reads_are_never_metered(self):
        """Only ingestion of paid evidence is gated. Reading back what a
        workspace already collected keeps working after a downgrade -- their
        data does not stop being theirs."""
        client = self._client("free")
        response = client.get("/api/anomalies", headers={
            "Authorization": "Bearer rlm_free.secret"})
        self.assertNotEqual(response.status_code, 402)

    def test_a_deployment_without_polar_meters_nothing(self):
        client = self._client("free", settings=None)
        response = client.post("/api/metadata-snapshots", json={}, headers={
            "Authorization": "Bearer rlm.secret"})
        self.assertNotEqual(response.status_code, 402)


class HistoryWindowTests(unittest.TestCase):
    """7 / 90 / unlimited, as a VISIBILITY bound.

    Nothing is deleted. The store is asked for reviews newer than a cutoff, and
    an upgrade brings the older ones straight back -- which is the whole reason
    this is a query bound and not a retention job.
    """

    class RecordingStore(FakeStore):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.since = "not called"

        def list_reviews(self, organization_id, repository_id, *,
                         environment=None, limit=25, offset=0, since=None):
            self.since = since
            return {"total": 0, "items": []}

    def _list(self, plan, settings=Settings()):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        from agent.api.routes import create_api_routes

        store = self.RecordingStore(
            billing={"t1": {"plan": plan, "subscription_status": "active"}},
            slugs={("acme", "warehouse-dbt"): "t1"})

        class Authenticator:
            def __init__(self, _store):
                pass

            def authenticate(self, token):
                return TokenPrincipal("acme", "warehouse-dbt", scope="operator_read")

        client = TestClient(Starlette(routes=create_api_routes(
            store_pool=StubPool(store), authenticator_factory=Authenticator,
            billing_settings=settings)))
        response = client.get("/api/reviews",
                              headers={"Authorization": "Bearer rlm.secret"})
        return response, store

    def _days(self, plan):
        response, store = self._list(plan)
        self.assertEqual(response.status_code, 200, response.text)
        if store.since is None:
            return None
        return round((datetime.now(timezone.utc) - store.since).days)

    def test_free_sees_seven_days(self):
        self.assertEqual(self._days("free"), 7)

    def test_starter_sees_ninety_days(self):
        self.assertEqual(self._days("starter"), 90)

    def test_pro_sees_everything(self):
        self.assertIsNone(self._days("pro"))

    def test_the_window_is_reported_so_the_ui_can_explain_the_cut_off(self):
        for plan, expected in (("free", 7), ("starter", 90), ("pro", None)):
            response, _ = self._list(plan)
            self.assertEqual(response.json()["history_window_days"], expected, plan)

    def test_a_deployment_without_polar_windows_nothing(self):
        response, store = self._list("free", settings=None)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(store.since)

    def test_the_window_is_a_query_bound_and_never_a_deletion(self):
        """A guard on the implementation: the retention path must not have
        acquired a DELETE."""
        import inspect

        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        source = inspect.getsource(PostgresLifecycleStore.list_reviews)
        # The docstring says "deletion" precisely because there is none, so
        # strip it before looking for the statement.
        code = source.replace(PostgresLifecycleStore.list_reviews.__doc__ or "", "")
        self.assertNotIn("DELETE", code.upper())
        self.assertIn("created_at >=", code)


class PlanCannotBeSpoofedTests(unittest.TestCase):
    """The plan comes from Relium's billing row. Nothing in the request."""

    def _post(self, path, **kwargs):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        from agent.api.routes import create_api_routes

        store = FakeStore(
            billing={"t1": {"plan": "free", "subscription_status": "active"}},
            slugs={("acme", "warehouse-dbt"): "t1"})

        class Authenticator:
            def __init__(self, _store):
                pass

            def authenticate(self, token):
                return TokenPrincipal("acme", "warehouse-dbt")

        client = TestClient(Starlette(routes=create_api_routes(
            store_pool=StubPool(store), authenticator_factory=Authenticator,
            billing_settings=Settings())))
        return client.post(path, **kwargs)

    def test_a_plan_in_the_body_changes_nothing(self):
        response = self._post(
            "/api/metadata-snapshots",
            json={"plan": "pro", "entitlements": {"warehouse_evidence": True},
                  "tenant_id": "t1", "subscription_status": "active"},
            headers={"Authorization": "Bearer rlm.secret"})
        self.assertEqual(response.status_code, 402)

    def test_a_plan_in_the_query_string_changes_nothing(self):
        response = self._post(
            "/api/metadata-snapshots?plan=pro&warehouse_evidence=true",
            json={}, headers={"Authorization": "Bearer rlm.secret"})
        self.assertEqual(response.status_code, 402)

    def test_a_plan_in_a_header_changes_nothing(self):
        response = self._post(
            "/api/metadata-snapshots", json={},
            headers={"Authorization": "Bearer rlm.secret",
                     "X-Relium-Plan": "pro",
                     "X-Plan": "pro"})
        self.assertEqual(response.status_code, 402)


if __name__ == "__main__":
    unittest.main()
