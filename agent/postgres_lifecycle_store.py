"""PostgreSQL authoritative lifecycle-store adapter.

Implements the same externally-visible contract as
``agent.sqlite_lifecycle_store.SQLiteLifecycleStore`` (used for deterministic
local unit tests) plus the extended continuous-pipeline entities: monitoring,
anomalies, incidents, RCA, lineage edges, KPI impact, outbox recovery and
dead-lettering, delivery journals and audit events.

This adapter refuses to operate without an explicitly supplied DSN and never
falls back to SQLite, an in-memory store, or filesystem/JSON storage.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from agent.lifecycle_models import ALLOWED_TRANSITIONS
from agent.metadata_evidence.change_request import normalize_remote_review_id
from agent.metadata_evidence.production_comparison import (
    ELIGIBLE_COMPLETENESS,
    ELIGIBLE_FRESHNESS,
)
from agent.postgres_migrate import apply_migrations

OUTBOX_LEASE_SECONDS = 300


class SnapshotConflict(ValueError):
    """An idempotency key was replayed with a different payload.

    Store-level so the persistence layer does not depend on the API layer;
    the HTTP boundary translates it to 409.
    """


class ManifestEvidenceConflict(ValueError):
    """A commit or idempotency key was replayed with different evidence."""


class TenantRepositoryConflict(ValueError):
    """A repository or an onboarding row is claimed by a different tenant.

    Store-level so the persistence layer stays independent of the API layer.
    The HTTP boundary renders it as the same non-disclosing 404 an unknown
    repository would produce: telling a caller that a private repository exists
    but belongs to somebody else is a disclosure in itself.
    """


class TenantBillingConflict(ValueError):
    """A Polar customer or subscription is already bound to another workspace.

    Store-level so the persistence layer stays independent of the API layer. The
    binding is never re-pointed to resolve this: moving a subscription between
    tenants is the one operation that would hand a paying customer's plan to
    somebody else, and no webhook payload is evidence that it should happen.
    """


class TenantRepositoryLimitReached(ValueError):
    """Connecting another repository would exceed the workspace's plan.

    Store-level because the limit has to be enforced in the same transaction
    that does the insert. Checking the count in the API layer and inserting
    afterwards leaves a window in which two concurrent selections both pass a
    check that neither of them then re-tests, and a Free workspace ends up with
    two repositories it cannot be charged for.
    """


class TenantInstallationConflict(ValueError):
    """A GitHub App installation is already bound to a different tenant.

    Store-level so the persistence layer does not depend on the API layer; the
    HTTP boundary translates it to a non-disclosing 409. Never re-point a
    binding to resolve this: doing so would hand one customer's repositories
    to another, which is precisely what a spoofed installation id would be
    trying to achieve.
    """



def _bounded_text(value, limit: int = 256):
    """Never persist an unbounded text value from a warehouse.

    Column extremes and metric text are evidence, not payload; anything longer
    than the bound is truncated with a marker so a large blob cannot be
    smuggled into the evidence plane.
    """
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


class PostgresLifecycleStore:
    provider = "postgresql"

    def __init__(self, dsn: str | None):
        if not dsn:
            raise RuntimeError("POSTGRES lifecycle store is BLOCKED BY CREDENTIALS")
        try:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg.types.json import Jsonb
        except ImportError as exc:
            raise RuntimeError("PostgreSQL lifecycle store requires psycopg") from exc
        self._psycopg = psycopg
        self._Jsonb = Jsonb
        # autocommit=True: every statement commits immediately and read-only
        # methods never leave the connection idle-in-transaction (which would
        # otherwise hold locks and starve concurrent DDL/other connections).
        # Multi-statement operations that must be atomic use an explicit
        # `with self.connection.transaction():` block instead.
        self.connection = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        apply_migrations(self.connection)

    # -- schema / tenant lifecycle -----------------------------------------

    def ensure_schema(self):
        apply_migrations(self.connection)

    def ensure_repository(self, organization_id, repository_id):
        """Create only the tenant/repository identity, without an environment."""
        with self.connection.transaction():
            self.connection.execute(
                "INSERT INTO organizations (organization_id) VALUES (%s) "
                "ON CONFLICT DO NOTHING",
                (organization_id,),
            )
            self.connection.execute(
                "INSERT INTO repositories (organization_id, repository_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (organization_id, repository_id),
            )

    def ensure_tenant(self, organization_id, repository_id, environment):
        with self.connection.transaction():
            self.connection.execute(
                "INSERT INTO organizations (organization_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (organization_id,),
            )
            self.connection.execute(
                "INSERT INTO repositories (organization_id, repository_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (organization_id, repository_id),
            )
            self.connection.execute(
                "INSERT INTO environments (organization_id, repository_id, environment, connected) "
                "VALUES (%s, %s, %s, TRUE) "
                "ON CONFLICT (organization_id, repository_id, environment) DO UPDATE SET connected = TRUE",
                (organization_id, repository_id, environment),
            )

    def disconnect_repository(self, organization_id, repository_id):
        self.connection.execute(
            "UPDATE environments SET connected = FALSE WHERE organization_id=%s AND repository_id=%s",
            (organization_id, repository_id),
        )

    # -- Clerk-identified tenants and onboarding ---------------------------
    #
    # The workspace-level tenant, keyed by an immutable Clerk organization id.
    # Distinct from the (organization_id, repository_id) pilot scope above,
    # which is a repository, not a customer.

    def tenant_by_clerk_organization(self, clerk_organization_id):
        """The tenant for one Clerk organization, or None.

        The lookup key is always the Clerk organization id from a verified
        token. There is deliberately no lookup by tenant_id alone: a caller
        naming a tenant is not evidence they belong to it, and offering that
        query would make it easy to write a route that trusts one.
        """
        row = self.connection.execute(
            "SELECT t.tenant_id, t.clerk_organization_id, t.organization_name, "
            "       t.role, t.team_size, t.created_at, t.updated_at, "
            "       s.current_step, s.completed_at "
            "FROM tenants t "
            "LEFT JOIN tenant_onboarding_state s ON s.tenant_id = t.tenant_id "
            "WHERE t.clerk_organization_id = %s",
            (clerk_organization_id,),
        ).fetchone()
        return dict(row) if row else None

    def upsert_tenant_for_clerk_organization(self, clerk_organization_id, *,
                                             organization_name, role=None,
                                             team_size=None):
        """Create the tenant for a Clerk organization, or update it in place.

        IDEMPOTENT, AND SAFE UNDER CONCURRENCY. The idempotency is the UNIQUE
        constraint on ``clerk_organization_id``, resolved by the database in
        one statement — not a read-then-write, which two simultaneous
        first-time requests would both pass before either inserted.

        ``ON CONFLICT DO UPDATE`` rather than ``DO NOTHING`` because the losing
        request must still receive the winning row: ``DO NOTHING`` returns no
        rows on conflict, and the caller would have to re-read, which reopens
        the race it just closed.

        The generated ``tenant_id`` on the conflicting path is discarded by the
        database. That is intentional: an identifier is cheap, and generating
        it before the insert is what lets this be a single atomic statement.
        """
        candidate_id = f"ten_{uuid.uuid4().hex}"
        with self.connection.transaction():
            row = self.connection.execute(
                "INSERT INTO tenants (tenant_id, clerk_organization_id, "
                "                     organization_name, role, team_size) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (clerk_organization_id) DO UPDATE SET "
                "    organization_name = EXCLUDED.organization_name, "
                "    role = EXCLUDED.role, "
                "    team_size = EXCLUDED.team_size, "
                "    updated_at = now() "
                "RETURNING tenant_id, clerk_organization_id, organization_name, "
                "          role, team_size, created_at, updated_at",
                (candidate_id, clerk_organization_id, organization_name, role, team_size),
            ).fetchone()
            tenant = dict(row)
            # The onboarding row is created with the tenant and never
            # separately, so a tenant can never exist without state to read.
            # DO NOTHING on replay: re-submitting the workspace step must not
            # rewind progress made on a later step.
            state = self.connection.execute(
                "INSERT INTO tenant_onboarding_state (tenant_id) VALUES (%s) "
                "ON CONFLICT (tenant_id) DO NOTHING "
                "RETURNING current_step, completed_at",
                (tenant["tenant_id"],),
            ).fetchone()
            if state is None:
                state = self.connection.execute(
                    "SELECT current_step, completed_at FROM tenant_onboarding_state "
                    "WHERE tenant_id = %s",
                    (tenant["tenant_id"],),
                ).fetchone()
        tenant.update(dict(state))
        return tenant

    def onboarding_state_for_tenant(self, tenant_id):
        """Durable onboarding progress for one tenant, or None."""
        row = self.connection.execute(
            "SELECT tenant_id, current_step, completed_at, created_at, updated_at "
            "FROM tenant_onboarding_state WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()
        return dict(row) if row else None

    # -- Polar billing, per tenant -----------------------------------------
    #
    # See migration 0018. The rules these methods exist to hold up: a
    # subscription belongs to exactly one workspace, an older webhook delivery
    # never overwrites a newer one, and a replayed delivery is a no-op.

    def billing_for_tenant(self, tenant_id):
        """This workspace's billing row, or None when it has never bought.

        None means the free plan. It is not an error and callers must not treat
        it as one — a tenant that has never reached checkout legitimately has no
        row, and creating an empty one to avoid the None would make "has a Polar
        customer" unanswerable.
        """
        row = self.connection.execute(
            "SELECT tenant_id, polar_customer_id, polar_subscription_id, "
            "       polar_product_id, plan, subscription_status, "
            "       current_period_end, cancel_at_period_end, past_due_at, "
            "       subscription_modified_at, created_at, updated_at "
            "FROM tenant_billing WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()
        return dict(row) if row else None

    def tenant_for_polar_customer(self, polar_customer_id):
        """The tenant already bound to one Polar customer id, or None.

        The webhook path's last-resort attribution, used only after the
        server-set external id and metadata on the payload came up empty. It
        reads a mapping RELIUM wrote; it never creates one.
        """
        row = self.connection.execute(
            "SELECT tenant_id FROM tenant_billing WHERE polar_customer_id = %s",
            (polar_customer_id,),
        ).fetchone()
        return row["tenant_id"] if row else None

    def upsert_billing_from_subscription(self, *, tenant_id, polar_customer_id,
                                         polar_subscription_id, polar_product_id,
                                         plan, subscription_status,
                                         current_period_end, cancel_at_period_end,
                                         past_due_at, subscription_modified_at):
        """Record one Polar subscription against one workspace.

        Returns ``applied``, ``stale`` or ``ignored``.

        ``ignored``  the tenant does not exist. A webhook naming a workspace
                     this deployment does not have is acknowledged, not created:
                     a tenant is minted by a verified Clerk session and by
                     nothing else, least of all by a payment provider's payload.

        ``stale``    a subscription object older than the one already stored.
                     Deliveries can arrive out of order, and without this guard
                     a retried `subscription.created` landing after
                     `subscription.revoked` would restore a plan Polar has
                     already ended. Enforced in the ON CONFLICT predicate rather
                     than by reading first and comparing in Python, which two
                     concurrent deliveries would both pass.

        ``applied``  the row now reflects this object.

        A subscription already recorded against a DIFFERENT tenant raises
        ``TenantBillingConflict``. The UNIQUE constraints are what detect it;
        the response is to refuse, never to re-point the row, because re-pointing
        is precisely what handing one customer's paid plan to another looks
        like.
        """
        with self.connection.transaction():
            exists = self.connection.execute(
                "SELECT 1 FROM tenants WHERE tenant_id = %s", (tenant_id,)
            ).fetchone()
            if exists is None:
                return "ignored"

            try:
                row = self.connection.execute(
                    "INSERT INTO tenant_billing ("
                    "    tenant_id, polar_customer_id, polar_subscription_id, "
                    "    polar_product_id, plan, subscription_status, "
                    "    current_period_end, cancel_at_period_end, past_due_at, "
                    "    subscription_modified_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (tenant_id) DO UPDATE SET "
                    "    polar_customer_id = COALESCE(EXCLUDED.polar_customer_id, "
                    "                                 tenant_billing.polar_customer_id), "
                    "    polar_subscription_id = EXCLUDED.polar_subscription_id, "
                    "    polar_product_id = EXCLUDED.polar_product_id, "
                    "    plan = EXCLUDED.plan, "
                    "    subscription_status = EXCLUDED.subscription_status, "
                    "    current_period_end = EXCLUDED.current_period_end, "
                    "    cancel_at_period_end = EXCLUDED.cancel_at_period_end, "
                    "    past_due_at = EXCLUDED.past_due_at, "
                    "    subscription_modified_at = EXCLUDED.subscription_modified_at, "
                    "    updated_at = now() "
                    "WHERE tenant_billing.subscription_modified_at IS NULL "
                    "   OR EXCLUDED.subscription_modified_at IS NULL "
                    "   OR EXCLUDED.subscription_modified_at "
                    "      >= tenant_billing.subscription_modified_at "
                    "RETURNING tenant_id",
                    (tenant_id, polar_customer_id, polar_subscription_id,
                     polar_product_id, plan, subscription_status,
                     current_period_end, bool(cancel_at_period_end), past_due_at,
                     subscription_modified_at),
                ).fetchone()
            except Exception as exc:
                if type(exc).__name__ == "UniqueViolation":
                    raise TenantBillingConflict(
                        "this Polar subscription belongs to another workspace"
                    ) from None
                raise
            # No row means the ON CONFLICT predicate refused the update, which
            # can only be the ordering guard.
            return "applied" if row is not None else "stale"

    def record_billing_webhook_delivery(self, *, delivery_id, event_type,
                                        tenant_id=None):
        """Claim one webhook delivery. False when it has already been seen.

        The de-duplication is the PRIMARY KEY on the delivery id, resolved by
        the database in one statement. A read-then-write would let two
        simultaneous retries of the same delivery both pass the read.
        """
        row = self.connection.execute(
            "INSERT INTO billing_webhook_deliveries "
            "(delivery_id, event_type, tenant_id) VALUES (%s, %s, %s) "
            "ON CONFLICT (delivery_id) DO NOTHING "
            "RETURNING delivery_id",
            (delivery_id, event_type, tenant_id),
        ).fetchone()
        return row is not None

    # -- GitHub App installation binding -----------------------------------
    #
    # See migration 0015. The rule these methods exist to hold up: an
    # installation is bound to a tenant only after three independent
    # verifications, and never because a browser supplied an installation id.

    def create_github_installation_state(self, *, state_hash, tenant_id,
                                         clerk_user_id, expires_at,
                                         purpose="github_app_install",
                                         created_at=None):
        """Mint one single-use installation-flow state.

        Only the hash is stored. The value itself lives in the redirect URL and
        in the customer's browser, and nowhere else, so a database disclosure
        cannot be replayed as a live installation flow.

        ``created_at`` is written explicitly rather than left to the column
        default. The two timestamps have to come from the SAME clock: the
        expiry is computed by the application, and if the row is stamped with
        the database's clock instead, any skew between them shifts the real
        lifetime. Far enough and the CHECK refuses the row; a little, and the
        state silently expires early or late. One clock, one consistent pair.
        """
        state_id = f"ist_{uuid.uuid4().hex}"
        self.connection.execute(
            "INSERT INTO github_installation_states "
            "(installation_state_id, state_hash, tenant_id, clerk_user_id, "
            " purpose, created_at, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, COALESCE(%s, now()), %s)",
            (state_id, state_hash, tenant_id, clerk_user_id, purpose,
             created_at, expires_at),
        )
        return state_id

    def consume_github_installation_state(self, state_hash, *, now,
                                          purpose="github_app_install"):
        """Claim a state exactly once, or return None.

        ###############################################################
        # SINGLE-USE IS ENFORCED BY THIS ONE STATEMENT.               #
        ###############################################################

        The guard lives in the WHERE clause, so the check and the claim are the
        same atomic operation. A read-then-write would let two concurrent
        redirects — a double-click, a retried request, or a deliberate replay —
        both observe an unconsumed state and both proceed. Here PostgreSQL
        serialises the UPDATE on the row and exactly one caller gets a row
        back; every other caller gets None and is refused.

        Expiry is part of the same guard rather than a separate check, so an
        expired state cannot be claimed by winning a race against a cleanup
        job that has not run yet.

        Returns the tenant and Clerk user recorded AT MINT TIME. Callers must
        use these and never any equivalent value from the request.
        """
        row = self.connection.execute(
            "UPDATE github_installation_states SET consumed_at = %s "
            "WHERE state_hash = %s AND purpose = %s "
            "  AND consumed_at IS NULL AND expires_at > %s "
            "RETURNING installation_state_id, tenant_id, clerk_user_id, "
            "          purpose, created_at, expires_at",
            (now, state_hash, purpose, now),
        ).fetchone()
        return dict(row) if row else None

    def github_installation_state(self, state_hash):
        """Read a state without consuming it. Tests and diagnostics only."""
        row = self.connection.execute(
            "SELECT installation_state_id, tenant_id, clerk_user_id, purpose, "
            "       created_at, expires_at, consumed_at "
            "FROM github_installation_states WHERE state_hash = %s",
            (state_hash,),
        ).fetchone()
        return dict(row) if row else None

    def delete_expired_github_installation_states(self, *, now):
        """Housekeeping. Consumed and expired states carry no further value."""
        return self.connection.execute(
            "DELETE FROM github_installation_states "
            "WHERE expires_at <= %s OR consumed_at IS NOT NULL",
            (now,),
        ).rowcount

    # -- Clerk user to GitHub user links -----------------------------------

    def upsert_clerk_github_identity(self, clerk_user_id, *, github_user_id,
                                     github_login, access_token=None,
                                     access_expires_at=None, refresh_token=None,
                                     refresh_expires_at=None):
        """Record that a Clerk user has proved control of a GitHub account.

        Only ever called after a completed GitHub OAuth exchange. Re-linking
        replaces the credential, which is what happens when a token is
        refreshed or the customer re-authorises.
        """
        row = self.connection.execute(
            "INSERT INTO clerk_github_identities "
            "(clerk_user_id, github_user_id, github_login, access_token, "
            " access_expires_at, refresh_token, refresh_expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (clerk_user_id) DO UPDATE SET "
            "  github_user_id = EXCLUDED.github_user_id, "
            "  github_login = EXCLUDED.github_login, "
            "  access_token = EXCLUDED.access_token, "
            "  access_expires_at = EXCLUDED.access_expires_at, "
            "  refresh_token = EXCLUDED.refresh_token, "
            "  refresh_expires_at = EXCLUDED.refresh_expires_at, "
            "  revoked_at = NULL, updated_at = now() "
            "RETURNING clerk_user_id, github_user_id, github_login, linked_at, "
            "          updated_at",
            (clerk_user_id, github_user_id, github_login, access_token,
             access_expires_at, refresh_token, refresh_expires_at),
        ).fetchone()
        return dict(row)

    def clerk_github_identity(self, clerk_user_id):
        """The GitHub identity linked to a Clerk user, or None.

        Returns the encrypted credential columns as stored. Decryption happens
        in the application, never in SQL, and the plaintext never leaves the
        server.
        """
        row = self.connection.execute(
            "SELECT clerk_user_id, github_user_id, github_login, access_token, "
            "       access_expires_at, refresh_token, refresh_expires_at, "
            "       linked_at, updated_at, revoked_at "
            "FROM clerk_github_identities "
            "WHERE clerk_user_id = %s AND revoked_at IS NULL",
            (clerk_user_id,),
        ).fetchone()
        return dict(row) if row else None

    def revoke_clerk_github_identity(self, clerk_user_id):
        """Drop the stored credential and mark the link revoked."""
        return self.connection.execute(
            "UPDATE clerk_github_identities "
            "SET revoked_at = now(), access_token = NULL, refresh_token = NULL, "
            "    updated_at = now() "
            "WHERE clerk_user_id = %s AND revoked_at IS NULL",
            (clerk_user_id,),
        ).rowcount

    # -- installation facts -------------------------------------------------

    def record_github_installation(self, github_installation_id, *,
                                   github_account_id, github_account_login,
                                   github_account_type, github_app_id=None,
                                   repository_selection=None, status="active",
                                   suspended_at=None, deleted_at=None):
        """Record what GitHub says about an installation. Tenant-agnostic.

        IDEMPOTENT, because webhook deliveries are retried and may arrive out
        of order, and because the Setup redirect and the webhook both land
        here. An upsert keyed on the installation id means a duplicate
        delivery is a no-op rather than a second row or an error.

        Says nothing about tenancy on purpose: a webhook payload contains
        nothing that identifies a Relium tenant, and guessing one from the
        account login would let anyone who can name a GitHub organization
        attach themselves to another customer's workspace.
        """
        row = self.connection.execute(
            "INSERT INTO github_installations "
            "(github_installation_id, github_app_id, github_account_id, "
            " github_account_login, github_account_type, repository_selection, "
            " status, suspended_at, deleted_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (github_installation_id) DO UPDATE SET "
            "  github_app_id = COALESCE(EXCLUDED.github_app_id, "
            "                           github_installations.github_app_id), "
            "  github_account_id = EXCLUDED.github_account_id, "
            "  github_account_login = EXCLUDED.github_account_login, "
            "  github_account_type = EXCLUDED.github_account_type, "
            "  repository_selection = COALESCE(EXCLUDED.repository_selection, "
            "                                  github_installations.repository_selection), "
            "  status = EXCLUDED.status, "
            "  suspended_at = EXCLUDED.suspended_at, "
            "  deleted_at = EXCLUDED.deleted_at, "
            "  updated_at = now() "
            "RETURNING github_installation_id, github_app_id, github_account_id, "
            "          github_account_login, github_account_type, "
            "          repository_selection, status, suspended_at, deleted_at, "
            "          first_seen_at, updated_at",
            (github_installation_id, github_app_id, github_account_id,
             github_account_login, github_account_type, repository_selection,
             status, suspended_at, deleted_at),
        ).fetchone()
        return dict(row)

    def github_installation(self, github_installation_id):
        row = self.connection.execute(
            "SELECT github_installation_id, github_app_id, github_account_id, "
            "       github_account_login, github_account_type, "
            "       repository_selection, status, suspended_at, deleted_at, "
            "       first_seen_at, updated_at "
            "FROM github_installations WHERE github_installation_id = %s",
            (github_installation_id,),
        ).fetchone()
        return dict(row) if row else None

    def set_github_installation_status(self, github_installation_id, status, *,
                                       suspended_at=None, deleted_at=None):
        """Lifecycle transition from a signature-verified webhook.

        Writes nothing if the installation is unknown: a status update is not a
        reason to invent a row, and doing so would let a delivery for another
        App's installation create one here.
        """
        return self.connection.execute(
            "UPDATE github_installations "
            "SET status = %s, suspended_at = %s, deleted_at = %s, updated_at = now() "
            "WHERE github_installation_id = %s",
            (status, suspended_at, deleted_at, github_installation_id),
        ).rowcount

    # -- the binding --------------------------------------------------------

    def bind_github_installation_to_tenant(self, github_installation_id, *,
                                           tenant_id, bound_by_clerk_user_id,
                                           verified_github_user_id,
                                           bound_via_state_id=None):
        """Attach a verified installation to a tenant, idempotently.

        Returns ``(binding, created)``. Re-running the same binding is a
        success with ``created=False`` — a customer who reloads the Setup
        redirect must not see an error.

        A DIFFERENT tenant claiming an already-bound installation is refused,
        not silently re-pointed. Re-pointing would hand one customer's
        repositories to another, and it is the exact outcome a spoofed
        installation id would be aiming for. The refusal is a
        ``TenantInstallationConflict``, which the HTTP layer renders as a
        non-disclosing 409.
        """
        row = self.connection.execute(
            "INSERT INTO tenant_github_installations "
            "(github_installation_id, tenant_id, bound_by_clerk_user_id, "
            " verified_github_user_id, bound_via_state_id) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (github_installation_id) DO UPDATE SET "
            "  updated_at = now() "
            # The guard is what makes a cross-tenant claim fail instead of
            # overwriting. On conflict with the SAME tenant this updates and
            # returns the row; with a different tenant the WHERE excludes it,
            # no row is returned, and the caller raises.
            "  WHERE tenant_github_installations.tenant_id = EXCLUDED.tenant_id "
            "RETURNING github_installation_id, tenant_id, bound_by_clerk_user_id, "
            "          verified_github_user_id, bound_via_state_id, bound_at, "
            "          updated_at, (xmax = 0) AS inserted",
            (github_installation_id, tenant_id, bound_by_clerk_user_id,
             verified_github_user_id, bound_via_state_id),
        ).fetchone()

        if row is None:
            existing = self.connection.execute(
                "SELECT tenant_id FROM tenant_github_installations "
                "WHERE github_installation_id = %s",
                (github_installation_id,),
            ).fetchone()
            if existing is not None:
                raise TenantInstallationConflict(
                    "this GitHub App installation is already connected to a "
                    "different Relium workspace")
            raise TenantInstallationConflict(
                "the installation binding could not be created")

        binding = dict(row)
        created = bool(binding.pop("inserted", False))
        return binding, created

    def tenant_github_installations(self, tenant_id, *, include_deleted=False):
        """Every installation bound to one tenant, newest binding first.

        Joined to the facts table so a caller gets account and status in one
        read. No credential or token is selected, because none is stored.
        """
        clause = "" if include_deleted else " AND i.status <> 'deleted'"
        rows = self.connection.execute(
            "SELECT b.github_installation_id, b.tenant_id, b.bound_at, "
            "       b.bound_by_clerk_user_id, b.verified_github_user_id, "
            "       i.github_app_id, i.github_account_id, i.github_account_login, "
            "       i.github_account_type, i.repository_selection, i.status, "
            "       i.suspended_at, i.deleted_at "
            "FROM tenant_github_installations b "
            "JOIN github_installations i "
            "  ON i.github_installation_id = b.github_installation_id "
            f"WHERE b.tenant_id = %s{clause} "
            "ORDER BY b.bound_at DESC, b.github_installation_id",
            (tenant_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def tenant_for_github_installation(self, github_installation_id):
        """Which tenant owns an installation, or None if it is unbound."""
        row = self.connection.execute(
            "SELECT tenant_id FROM tenant_github_installations "
            "WHERE github_installation_id = %s",
            (github_installation_id,),
        ).fetchone()
        return row["tenant_id"] if row else None

    # -- repository selection, dbt configuration, CI state ------------------
    #
    # See migration 0016. Every method here takes a tenant_id and includes it
    # in the WHERE clause, so an id that belongs to another tenant matches no
    # row rather than being corrected after the fact.

    def tenant_for_repository_slug(self, organization_id, repository_id):
        """The tenant that connected one GitHub owner/name pair, or None.

        The join between a service token's scope — which names a repository by
        owner login and name — and billing, which is keyed by tenant. Reads a
        mapping onboarding wrote; never creates one.
        """
        row = self.connection.execute(
            "SELECT tenant_id FROM tenant_repositories "
            "WHERE owner_login = %s AND name = %s LIMIT 1",
            (organization_id, repository_id),
        ).fetchone()
        return row["tenant_id"] if row else None

    def count_tenant_repositories(self, tenant_id):
        row = self.connection.execute(
            "SELECT count(*) AS n FROM tenant_repositories WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()
        return int(row["n"]) if row else 0

    def select_tenant_repository(self, github_repository_id, *, tenant_id,
                                 github_installation_id, owner_login, name,
                                 default_branch=None, private=None,
                                 dbt_detected=None, dbt_project_dir=None,
                                 dbt_checked_at=None, repository_limit=None):
        """Record the tenant's chosen repository, idempotently.

        Re-selecting the same repository refreshes the display metadata and the
        detection result without disturbing the dbt configuration or the CI
        token state — a customer stepping back and forward must not lose work.

        A repository already claimed by a DIFFERENT tenant raises rather than
        being re-pointed. Re-pointing would move a configured repository
        between customers, which is exactly what a spoofed id would attempt.

        ``repository_limit`` is the plan's allowance; None means unlimited.
        It is enforced HERE, inside the transaction that inserts, and only
        against repositories that are not already this tenant's — re-selecting
        one they already have is never a new connection and must keep working
        for a workspace that is over its allowance after a downgrade.
        """
        if repository_limit is None:
            return self._upsert_tenant_repository(
                github_repository_id, tenant_id=tenant_id,
                github_installation_id=github_installation_id,
                owner_login=owner_login, name=name,
                default_branch=default_branch, private=private,
                dbt_detected=dbt_detected, dbt_project_dir=dbt_project_dir,
                dbt_checked_at=dbt_checked_at)

        with self.connection.transaction():
            # Serialises concurrent selections for THIS tenant against each
            # other, so the count below cannot be read by two transactions that
            # then both insert. Other tenants are unaffected: the lock is one
            # row in `tenants`.
            self.connection.execute(
                "SELECT 1 FROM tenants WHERE tenant_id = %s FOR UPDATE",
                (tenant_id,),
            ).fetchone()
            already_theirs = self.connection.execute(
                "SELECT 1 FROM tenant_repositories "
                "WHERE tenant_id = %s AND github_repository_id = %s",
                (tenant_id, github_repository_id),
            ).fetchone()
            if already_theirs is None:
                connected = self.count_tenant_repositories(tenant_id)
                if connected + 1 > repository_limit:
                    raise TenantRepositoryLimitReached(
                        "this workspace has reached the number of repositories "
                        "its plan includes")
            return self._upsert_tenant_repository(
                github_repository_id, tenant_id=tenant_id,
                github_installation_id=github_installation_id,
                owner_login=owner_login, name=name,
                default_branch=default_branch, private=private,
                dbt_detected=dbt_detected, dbt_project_dir=dbt_project_dir,
                dbt_checked_at=dbt_checked_at)

    def _upsert_tenant_repository(self, github_repository_id, *, tenant_id,
                                  github_installation_id, owner_login, name,
                                  default_branch, private, dbt_detected,
                                  dbt_project_dir, dbt_checked_at):
        row = self.connection.execute(
            "INSERT INTO tenant_repositories "
            "(github_repository_id, tenant_id, github_installation_id, "
            " owner_login, name, default_branch, private, dbt_detected, "
            " dbt_project_dir, dbt_checked_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (github_repository_id) DO UPDATE SET "
            "  github_installation_id = EXCLUDED.github_installation_id, "
            "  owner_login = EXCLUDED.owner_login, "
            "  name = EXCLUDED.name, "
            "  default_branch = EXCLUDED.default_branch, "
            "  private = EXCLUDED.private, "
            "  dbt_detected = EXCLUDED.dbt_detected, "
            "  dbt_project_dir = EXCLUDED.dbt_project_dir, "
            "  dbt_checked_at = EXCLUDED.dbt_checked_at, "
            "  updated_at = now() "
            # The guard. On conflict with the SAME tenant this updates and
            # returns; with a different tenant no row is returned and the
            # caller raises.
            "  WHERE tenant_repositories.tenant_id = EXCLUDED.tenant_id "
            "RETURNING github_repository_id, tenant_id, github_installation_id, "
            "          owner_login, name, default_branch, private, "
            "          dbt_detected, dbt_project_dir, dbt_checked_at, "
            "          project_dir, manifest_path, enforcement_mode, "
            "          ci_token_id, ci_token_issued_at, ci_token_delivery, "
            "          selected_at, configured_at",
            (github_repository_id, tenant_id, github_installation_id,
             owner_login, name, default_branch, private, dbt_detected,
             dbt_project_dir, dbt_checked_at),
        ).fetchone()

        if row is None:
            raise TenantRepositoryConflict(
                "this repository is already connected to a different Relium "
                "workspace")
        return dict(row)

    def tenant_repository(self, tenant_id, github_repository_id):
        """One repository, scoped to the tenant. None if it is not theirs."""
        row = self.connection.execute(
            "SELECT github_repository_id, tenant_id, github_installation_id, "
            "       owner_login, name, default_branch, private, dbt_detected, "
            "       dbt_project_dir, dbt_checked_at, project_dir, manifest_path, "
            "       enforcement_mode, ci_token_id, ci_token_issued_at, "
            "       ci_token_delivery, selected_at, configured_at "
            "FROM tenant_repositories "
            "WHERE tenant_id = %s AND github_repository_id = %s",
            (tenant_id, github_repository_id),
        ).fetchone()
        return dict(row) if row else None

    def tenant_repositories(self, tenant_id):
        rows = self.connection.execute(
            "SELECT github_repository_id, tenant_id, github_installation_id, "
            "       owner_login, name, default_branch, private, dbt_detected, "
            "       dbt_project_dir, project_dir, manifest_path, "
            "       enforcement_mode, ci_token_id, ci_token_delivery, "
            "       selected_at, configured_at "
            "FROM tenant_repositories WHERE tenant_id = %s "
            "ORDER BY owner_login, name",
            (tenant_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def tenant_repository_detections(self, tenant_id):
        rows = self.connection.execute(
            "SELECT github_repository_id, tenant_id, github_installation_id, "
            "owner_login, name, default_branch, private, dbt_detected, "
            "dbt_project_dir, dbt_checked_at, dbt_checked_commit_sha "
            "FROM tenant_repository_dbt_detection WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def upsert_tenant_repository_detection(self, *, tenant_id,
                                           github_repository_id,
                                           github_installation_id, owner_login,
                                           name, default_branch, private,
                                           dbt_detected, dbt_project_dir,
                                           dbt_checked_at,
                                           dbt_checked_commit_sha):
        self.connection.execute(
            "INSERT INTO tenant_repository_dbt_detection "
            "(tenant_id, github_repository_id, github_installation_id, "
            " owner_login, name, default_branch, private, dbt_detected, "
            " dbt_project_dir, dbt_checked_at, dbt_checked_commit_sha) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (tenant_id, github_repository_id) DO UPDATE SET "
            " github_installation_id = EXCLUDED.github_installation_id, "
            " owner_login = EXCLUDED.owner_login, name = EXCLUDED.name, "
            " default_branch = EXCLUDED.default_branch, private = EXCLUDED.private, "
            " dbt_detected = EXCLUDED.dbt_detected, "
            " dbt_project_dir = EXCLUDED.dbt_project_dir, "
            " dbt_checked_at = EXCLUDED.dbt_checked_at, "
            " dbt_checked_commit_sha = EXCLUDED.dbt_checked_commit_sha",
            (tenant_id, github_repository_id, github_installation_id,
             owner_login, name, default_branch, private, dbt_detected,
             dbt_project_dir, dbt_checked_at, dbt_checked_commit_sha),
        )
        self.connection.commit()

    def configured_tenant_repository(self, tenant_id):
        """The tenant's configured repository, if there is one.

        Onboarding configures one repository; this is what completion checks
        and what the onboarding state reports.
        """
        row = self.connection.execute(
            "SELECT github_repository_id, tenant_id, github_installation_id, "
            "       owner_login, name, default_branch, private, project_dir, "
            "       manifest_path, enforcement_mode, ci_token_id, "
            "       ci_token_issued_at, ci_token_delivery, configured_at "
            "FROM tenant_repositories "
            "WHERE tenant_id = %s AND manifest_path IS NOT NULL "
            "ORDER BY configured_at DESC NULLS LAST, github_repository_id "
            "LIMIT 1",
            (tenant_id,),
        ).fetchone()
        return dict(row) if row else None

    def configure_tenant_repository(self, github_repository_id, *, tenant_id,
                                    project_dir, manifest_path,
                                    enforcement_mode, configured_at):
        """Persist the dbt configuration. Idempotent by being a plain UPDATE.

        The tenant is in the WHERE clause, so a repository belonging to someone
        else matches nothing and the caller sees the same not-found it would
        see for an id that does not exist.
        """
        row = self.connection.execute(
            "UPDATE tenant_repositories "
            "SET project_dir = %s, manifest_path = %s, enforcement_mode = %s, "
            "    configured_at = %s, updated_at = now() "
            "WHERE tenant_id = %s AND github_repository_id = %s "
            "RETURNING github_repository_id, tenant_id, owner_login, name, "
            "          project_dir, manifest_path, enforcement_mode, "
            "          ci_token_id, ci_token_delivery, configured_at",
            (project_dir, manifest_path, enforcement_mode, configured_at,
             tenant_id, github_repository_id),
        ).fetchone()
        return dict(row) if row else None

    def record_tenant_repository_ci_token(self, github_repository_id, *,
                                          tenant_id, ci_token_id, delivery,
                                          issued_at):
        """Record THAT a CI token exists, and how it was delivered.

        The token id is the non-secret half; the secret is never stored here or
        anywhere else. issue_ci_token keeps only sha256(secret) on
        api_service_tokens, which is the sole record of it.
        """
        row = self.connection.execute(
            "UPDATE tenant_repositories "
            "SET ci_token_id = %s, ci_token_delivery = %s, "
            "    ci_token_issued_at = %s, updated_at = now() "
            "WHERE tenant_id = %s AND github_repository_id = %s "
            "RETURNING github_repository_id, ci_token_id, ci_token_delivery, "
            "          ci_token_issued_at",
            (ci_token_id, delivery, issued_at, tenant_id, github_repository_id),
        ).fetchone()
        return dict(row) if row else None

    def complete_tenant_onboarding(self, tenant_id, *, completed_at,
                                   repository_id=None, clerk_user_id=None):
        """Mark onboarding finished. Safe under concurrent completion.

        ``completed_at IS NULL`` in the WHERE clause is what makes this
        single-shot: two simultaneous completions race on the row and exactly
        one performs the write. The loser reads the winner's values and returns
        them, so both callers see the same completion rather than one seeing an
        error for having been a millisecond late.

        current_step is moved to 'ready' in the same statement, because the
        CHECK constraint in migration 0014 refuses a completed row that still
        points at an unfinished step.
        """
        row = self.connection.execute(
            "UPDATE tenant_onboarding_state "
            "SET completed_at = %s, current_step = 'ready', "
            "    completed_repository_id = %s, completed_by_clerk_user_id = %s, "
            "    updated_at = now() "
            "WHERE tenant_id = %s AND completed_at IS NULL "
            "RETURNING tenant_id, completed_at, completed_repository_id, "
            "          completed_by_clerk_user_id",
            (completed_at, repository_id, clerk_user_id, tenant_id),
        ).fetchone()
        if row is not None:
            record = dict(row)
            record["created"] = True
            return record

        existing = self.connection.execute(
            "SELECT tenant_id, completed_at, completed_repository_id, "
            "       completed_by_clerk_user_id "
            "FROM tenant_onboarding_state WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()
        if existing is None:
            raise TenantRepositoryConflict(
                "there is no onboarding state for this workspace")
        record = dict(existing)
        record["created"] = False
        return record

    def delete_tenant(self, organization_id):
        now = datetime.now(timezone.utc)
        with self.connection.transaction():
            cur = self.connection.execute(
                "INSERT INTO retention_tombstones (organization_id, deleted_at) VALUES (%s, %s) "
                "ON CONFLICT (organization_id) DO UPDATE SET deleted_at = EXCLUDED.deleted_at "
                "RETURNING deleted_at",
                (organization_id, now),
            )
            row = cur.fetchone()
            # Junction tables now carry the tenant themselves, so they are deleted
            # by the same direct predicate as every other tenant-scoped table.
            for table in (
                "rca_evidence_links", "lineage_edges",
                "rca_reports", "incidents", "anomalies",
                "monitoring_observations", "metadata_baselines", "kpi_impact",
                "lineage_records", "outbox_dead_letters", "outbox_events",
                "deployment_transitions", "deployments", "evidence", "configuration_versions",
                "manifest_evidence", "delivery_journal", "environments", "repositories",
            ):
                self.connection.execute(f"DELETE FROM {table} WHERE organization_id=%s", (organization_id,))
            # Audit events are retained across tenant deletion for compliance; they are
            # keyed by the tombstoned organization_id so they remain attributable.
        return {"organization_id": organization_id, "deleted_at": row["deleted_at"].isoformat()}

    def _tenant(self, organization_id, repository_id, environment, *, allow_disconnected=False):
        row = self.connection.execute(
            "SELECT connected FROM environments WHERE organization_id=%s AND repository_id=%s AND environment=%s",
            (organization_id, repository_id, environment),
        ).fetchone()
        if not row or (not allow_disconnected and not row["connected"]):
            raise ValueError("Unknown, disconnected, or unauthorized tenant")

    # -- configuration / policy / detector versions -------------------------

    def record_versions(self, organization_id, repository_id, environment, *, policy, detector, threshold):
        self._tenant(organization_id, repository_id, environment)
        self.connection.execute(
            "INSERT INTO configuration_versions "
            "(organization_id, repository_id, environment, policy_version, detector_version, threshold_version) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (organization_id, repository_id, environment, policy, detector, threshold),
        )
        return {"policy_version": policy, "detector_version": detector, "threshold_version": threshold}

    def latest_versions(self, organization_id, repository_id, environment):
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        row = self.connection.execute(
            "SELECT policy_version, detector_version, threshold_version FROM configuration_versions "
            "WHERE organization_id=%s AND repository_id=%s AND environment=%s "
            "ORDER BY created_at DESC, configuration_version_id DESC LIMIT 1",
            (organization_id, repository_id, environment),
        ).fetchone()
        return dict(row) if row else {}

    # -- evidence -------------------------------------------------------------

    def append_evidence(self, organization_id, repository_id, environment, payload, *, evidence_id=None):
        self._tenant(organization_id, repository_id, environment)
        evidence_id = evidence_id or str(uuid.uuid4())
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        with self.connection.transaction():
            existing = self.connection.execute(
                "SELECT 1 FROM evidence WHERE evidence_id=%s", (evidence_id,)
            ).fetchone()
            if existing:
                raise ValueError("Evidence references are immutable")
            self.connection.execute(
                "INSERT INTO evidence (evidence_id, organization_id, repository_id, environment, payload, content_hash) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (evidence_id, organization_id, repository_id, environment, self._Jsonb(payload), digest),
            )
        return {"evidence_id": evidence_id, "hash": digest, "payload": payload}

    def list_evidence(self, organization_id, repository_id, environment):
        try:
            self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        except ValueError:
            tombstoned = self.connection.execute(
                "SELECT 1 FROM retention_tombstones WHERE organization_id=%s", (organization_id,)
            ).fetchone()
            if not tombstoned:
                raise
            return []
        rows = self.connection.execute(
            "SELECT evidence_id, content_hash AS hash, payload FROM evidence "
            "WHERE organization_id=%s AND repository_id=%s AND environment=%s",
            (organization_id, repository_id, environment),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- deployment lifecycle --------------------------------------------------

    def create_deployment(self, organization_id, repository_id, environment, payload):
        self._tenant(organization_id, repository_id, environment)
        deployment_id = payload["deployment_id"]
        # Scoped: an identifier owned by another tenant must not resolve here.
        existing = self.connection.execute(
            "SELECT payload, status FROM deployments "
            "WHERE organization_id=%s AND repository_id=%s AND deployment_id=%s",
            (organization_id, repository_id, deployment_id),
        ).fetchone()
        if existing:
            return {"deployment_id": deployment_id, **existing["payload"], "status": existing["status"]}
        with self.connection.transaction():
            self.connection.execute(
                "INSERT INTO deployments "
                "(deployment_id, organization_id, repository_id, environment, reviewed_sha, merge_sha, manifest_hash, payload, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    deployment_id, organization_id, repository_id, environment,
                    payload.get("reviewed_sha"), payload.get("merge_sha"), payload.get("manifest_hash"),
                    self._Jsonb(payload), "reviewed",
                ),
            )
            self.connection.execute(
                "INSERT INTO outbox_events (event_id, organization_id, repository_id, environment, "
                "subject_type, subject_id, deployment_id, event_type, payload) "
                "VALUES (%s, %s, %s, %s, 'deployment', %s, %s, %s, %s)",
                (str(uuid.uuid4()), organization_id, repository_id, environment, deployment_id,
                 deployment_id, "deployment.reviewed", self._Jsonb(payload)),
            )
        return {"deployment_id": deployment_id, **payload, "status": "reviewed"}

    def append_transition(self, organization_id, repository_id, environment, deployment_id, to_status):
        self._tenant(organization_id, repository_id, environment)
        row = self.connection.execute(
            "SELECT status FROM deployments WHERE deployment_id=%s AND organization_id=%s "
            "AND repository_id=%s AND environment=%s",
            (deployment_id, organization_id, repository_id, environment),
        ).fetchone()
        if not row:
            raise ValueError("Unknown deployment")
        if to_status == row["status"]:
            return
        if to_status not in ALLOWED_TRANSITIONS.get(row["status"], set()):
            raise ValueError(f"Invalid deployment transition {row['status']} -> {to_status}")
        with self.connection.transaction():
            next_seq = self.connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next FROM deployment_transitions "
                "WHERE organization_id=%s AND repository_id=%s AND deployment_id=%s",
                (organization_id, repository_id, deployment_id),
            ).fetchone()["next"]
            self.connection.execute(
                "UPDATE deployments SET status=%s, updated_at=now() "
                "WHERE organization_id=%s AND repository_id=%s AND deployment_id=%s",
                (to_status, organization_id, repository_id, deployment_id),
            )
            self.connection.execute(
                "INSERT INTO deployment_transitions "
                "(deployment_id, organization_id, repository_id, environment, from_status, to_status, sequence) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (deployment_id, organization_id, repository_id, environment, row["status"], to_status, next_seq),
            )
            self.connection.execute(
                # dedup_key defaults to '' for deployment events, so the
                # conflict target must name it to match the unique index.
                # Behaviour is unchanged: one event per (deployment, status).
                "INSERT INTO outbox_events (event_id, organization_id, repository_id, environment, "
                "subject_type, subject_id, deployment_id, event_type, payload) "
                "VALUES (%s, %s, %s, %s, 'deployment', %s, %s, %s, %s) "
                "ON CONFLICT (organization_id, repository_id, environment, subject_type, "
                "subject_id, event_type, dedup_key) DO NOTHING",
                (str(uuid.uuid4()), organization_id, repository_id, environment, deployment_id,
                 deployment_id, f"deployment.{to_status}",
                 self._Jsonb({"deployment_id": deployment_id, "status": to_status})),
            )

    def transitions(self, organization_id, repository_id, environment, deployment_id):
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        rows = self.connection.execute(
            "SELECT * FROM deployment_transitions "
            "WHERE organization_id=%s AND repository_id=%s AND deployment_id=%s ORDER BY sequence",
            (organization_id, repository_id, deployment_id),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- transactional outbox --------------------------------------------------

    def claim_outbox(self, organization_id, repository_id, environment, worker):
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        self._recover_expired_claims(organization_id, repository_id, environment)
        # The SELECT ... FOR UPDATE SKIP LOCKED and the UPDATE that claims the
        # winning row must share one transaction: the row lock only prevents a
        # concurrent claim for as long as the transaction holding it is open.
        with self.connection.transaction():
            row = self.connection.execute(
                "SELECT * FROM outbox_events WHERE organization_id=%s AND repository_id=%s AND environment=%s "
                "AND state='PENDING' AND next_attempt_at <= now() "
                "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED",
                (organization_id, repository_id, environment),
            ).fetchone()
            if not row:
                return None
            lease_expires = datetime.now(timezone.utc) + timedelta(seconds=OUTBOX_LEASE_SECONDS)
            self.connection.execute(
                "UPDATE outbox_events SET state='CLAIMED', lease_owner=%s, lease_expires_at=%s, attempts=attempts+1 "
                "WHERE organization_id=%s AND repository_id=%s AND event_id=%s AND state='PENDING'",
                (worker, lease_expires, organization_id, repository_id, row["event_id"]),
            )
        return dict(row)

    def _recover_expired_claims(self, organization_id, repository_id, environment):
        self.connection.execute(
            "UPDATE outbox_events SET state='PENDING', lease_owner=NULL, lease_expires_at=NULL "
            "WHERE organization_id=%s AND repository_id=%s AND environment=%s "
            "AND state='CLAIMED' AND lease_expires_at < now()",
            (organization_id, repository_id, environment),
        )

    def complete_outbox(self, organization_id, repository_id, event_id):
        self.connection.execute(
            "UPDATE outbox_events SET state='COMPLETED', completed_at=now() "
            "WHERE organization_id=%s AND repository_id=%s AND event_id=%s AND state='CLAIMED'",
            (organization_id, repository_id, event_id),
        )

    def fail_outbox(self, organization_id, repository_id, event_id, *, error,
                    max_attempts=5, retry_backoff_seconds=30):
        with self.connection.transaction():
            row = self.connection.execute(
                "SELECT * FROM outbox_events "
                "WHERE organization_id=%s AND repository_id=%s AND event_id=%s FOR UPDATE",
                (organization_id, repository_id, event_id),
            ).fetchone()
            if not row or row["state"] in ("DEAD_LETTER", "COMPLETED"):
                return
            if row["attempts"] >= max_attempts:
                self._dead_letter(row, error)
            else:
                next_attempt = datetime.now(timezone.utc) + timedelta(seconds=retry_backoff_seconds)
                self.connection.execute(
                    "UPDATE outbox_events SET state='PENDING', lease_owner=NULL, lease_expires_at=NULL, "
                    "next_attempt_at=%s, last_error=%s "
                    "WHERE organization_id=%s AND repository_id=%s AND event_id=%s",
                    (next_attempt, str(error)[:2000], organization_id, repository_id, event_id),
                )

    def _dead_letter(self, row, error):
        self.connection.execute(
            "INSERT INTO outbox_dead_letters "
            "(event_id, organization_id, repository_id, environment, deployment_id, event_type, payload, attempts, last_error) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (row["event_id"], row["organization_id"], row["repository_id"], row["environment"],
             row["deployment_id"], row["event_type"], self._Jsonb(row["payload"]), row["attempts"], str(error)[:2000]),
        )
        self.connection.execute(
            "UPDATE outbox_events SET state='DEAD_LETTER', last_error=%s "
            "WHERE organization_id=%s AND repository_id=%s AND event_id=%s",
            (str(error)[:2000], row["organization_id"], row["repository_id"], row["event_id"]),
        )

    def dead_letters(self, organization_id, repository_id, environment):
        rows = self.connection.execute(
            "SELECT * FROM outbox_dead_letters WHERE organization_id=%s AND repository_id=%s AND environment=%s "
            "ORDER BY created_at",
            (organization_id, repository_id, environment),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- monitoring / anomalies --------------------------------------------------

    def record_metadata_baseline(self, organization_id, repository_id, environment, model, baseline):
        self._tenant(organization_id, repository_id, environment)
        self.connection.execute(
            "INSERT INTO metadata_baselines (organization_id, repository_id, environment, model, baseline) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (organization_id, repository_id, environment, model) "
            "DO UPDATE SET baseline=EXCLUDED.baseline, created_at=now()",
            (organization_id, repository_id, environment, model, self._Jsonb(baseline)),
        )

    def append_observation(self, organization_id, repository_id, environment, *, deployment_id, model, metric, payload, observation_id=None):
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        observation_id = observation_id or str(uuid.uuid4())
        self.connection.execute(
            "INSERT INTO monitoring_observations "
            "(observation_id, organization_id, repository_id, environment, deployment_id, model, metric, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (observation_id, organization_id, repository_id, environment, deployment_id, model, metric, self._Jsonb(payload)),
        )
        return observation_id

    def observations(self, organization_id, repository_id, environment, *, deployment_id=None):
        if deployment_id:
            rows = self.connection.execute(
                "SELECT * FROM monitoring_observations WHERE organization_id=%s AND repository_id=%s "
                "AND environment=%s AND deployment_id=%s ORDER BY observed_at",
                (organization_id, repository_id, environment, deployment_id),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM monitoring_observations WHERE organization_id=%s AND repository_id=%s "
                "AND environment=%s ORDER BY observed_at",
                (organization_id, repository_id, environment),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_anomaly(self, organization_id, repository_id, environment, *, deployment_id, kind, payload, anomaly_id=None):
        """Idempotent: a second anomaly of the same kind for the same deployment returns the first."""
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        anomaly_id = anomaly_id or str(uuid.uuid4())
        with self.connection.transaction():
            row = self.connection.execute(
                "INSERT INTO anomalies (anomaly_id, organization_id, repository_id, environment, deployment_id, kind, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (organization_id, repository_id, environment, deployment_id, kind) DO NOTHING "
                "RETURNING *",
                (anomaly_id, organization_id, repository_id, environment, deployment_id, kind, self._Jsonb(payload)),
            ).fetchone()
            if row is None:
                row = self.connection.execute(
                    "SELECT * FROM anomalies WHERE organization_id=%s AND repository_id=%s AND environment=%s "
                    "AND deployment_id=%s AND kind=%s",
                    (organization_id, repository_id, environment, deployment_id, kind),
                ).fetchone()
        return dict(row)

    def anomalies(self, organization_id, repository_id, environment, *, deployment_id=None):
        if deployment_id:
            rows = self.connection.execute(
                "SELECT * FROM anomalies WHERE organization_id=%s AND repository_id=%s AND environment=%s "
                "AND deployment_id=%s ORDER BY created_at",
                (organization_id, repository_id, environment, deployment_id),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM anomalies WHERE organization_id=%s AND repository_id=%s AND environment=%s ORDER BY created_at",
                (organization_id, repository_id, environment),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- lineage / KPI impact --------------------------------------------------

    def record_lineage(self, organization_id, repository_id, environment, model, payload, *, edges=(), completeness=None, lineage_id=None):
        self._tenant(organization_id, repository_id, environment)
        lineage_id = lineage_id or str(uuid.uuid4())
        with self.connection.transaction():
            self.connection.execute(
                "INSERT INTO lineage_records (lineage_id, organization_id, repository_id, environment, model, payload, completeness) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (organization_id, repository_id, environment, lineage_id) DO NOTHING",
                (lineage_id, organization_id, repository_id, environment, model, self._Jsonb(payload), completeness),
            )
            for upstream, downstream in edges:
                self.connection.execute(
                    "INSERT INTO lineage_edges "
                    "(lineage_id, upstream_model, downstream_model, organization_id, repository_id) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (lineage_id, upstream, downstream, organization_id, repository_id),
                )
        return lineage_id

    def list_lineage(self, organization_id, repository_id, environment):
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        rows = self.connection.execute(
            "SELECT lineage_id, payload FROM lineage_records "
            "WHERE organization_id=%s AND repository_id=%s AND environment=%s",
            (organization_id, repository_id, environment),
        ).fetchall()
        return [dict(row["payload"], lineage_id=row["lineage_id"]) for row in rows]

    def record_kpi_impact(self, organization_id, repository_id, environment, *, deployment_id, kpi_name, impact, kpi_impact_id=None):
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        kpi_impact_id = kpi_impact_id or str(uuid.uuid4())
        self.connection.execute(
            "INSERT INTO kpi_impact (kpi_impact_id, organization_id, repository_id, environment, deployment_id, kpi_name, impact) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (kpi_impact_id, organization_id, repository_id, environment, deployment_id, kpi_name, self._Jsonb(impact)),
        )
        return kpi_impact_id

    def kpi_impacts(self, organization_id, repository_id, environment, *, deployment_id=None):
        if deployment_id:
            rows = self.connection.execute(
                "SELECT * FROM kpi_impact WHERE organization_id=%s AND repository_id=%s AND environment=%s "
                "AND deployment_id=%s ORDER BY created_at",
                (organization_id, repository_id, environment, deployment_id),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM kpi_impact WHERE organization_id=%s AND repository_id=%s AND environment=%s ORDER BY created_at",
                (organization_id, repository_id, environment),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- incidents / RCA --------------------------------------------------

    def create_incident(self, organization_id, repository_id, environment, *, deployment_id, anomaly_id, incident_id=None):
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        incident_id = incident_id or str(uuid.uuid4())
        with self.connection.transaction():
            row = self.connection.execute(
                "INSERT INTO incidents (incident_id, organization_id, repository_id, environment, deployment_id, anomaly_id, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'open') "
                "ON CONFLICT (organization_id, repository_id, incident_id) DO NOTHING RETURNING *",
                (incident_id, organization_id, repository_id, environment, deployment_id, anomaly_id),
            ).fetchone()
            if row is None:
                row = self.connection.execute(
                    "SELECT * FROM incidents "
                    "WHERE organization_id=%s AND repository_id=%s AND incident_id=%s",
                    (organization_id, repository_id, incident_id),
                ).fetchone()
        return dict(row)

    def update_incident_status(self, organization_id, repository_id, incident_id, status):
        """Idempotent: setting the same status twice is a no-op, not an error."""
        self.connection.execute(
            "UPDATE incidents SET status=%s, updated_at=now() "
            "WHERE organization_id=%s AND repository_id=%s AND incident_id=%s AND status != %s",
            (status, organization_id, repository_id, incident_id, status),
        )

    def get_incident(self, organization_id, repository_id, incident_id):
        row = self.connection.execute(
            "SELECT * FROM incidents WHERE organization_id=%s AND repository_id=%s AND incident_id=%s",
            (organization_id, repository_id, incident_id),
        ).fetchone()
        return dict(row) if row else None

    def create_rca(self, incident_id, organization_id, repository_id, environment, *, status, primary_cause=None,
                    alternative_causes=(), contributing_factors=(), downstream_symptoms=(),
                    unrelated_concurrent_changes=(), confidence=None, unevaluated_evidence=(),
                    evidence_links=(), rca_id=None):
        """Idempotent: only one COMPLETED RCA is retained per incident (a unique partial index enforces it)."""
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        rca_id = rca_id or str(uuid.uuid4())
        try:
            # The insert and its evidence links share a transaction so a link
            # failure can't leave an RCA report with partial evidence attached.
            with self.connection.transaction():
                row = self.connection.execute(
                    "INSERT INTO rca_reports "
                    "(rca_id, incident_id, organization_id, repository_id, environment, status, primary_cause, "
                    "alternative_causes, contributing_factors, downstream_symptoms, unrelated_concurrent_changes, "
                    "confidence, unevaluated_evidence) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                    (
                        rca_id, incident_id, organization_id, repository_id, environment, status,
                        self._Jsonb(primary_cause) if primary_cause is not None else None,
                        self._Jsonb(list(alternative_causes)), self._Jsonb(list(contributing_factors)),
                        self._Jsonb(list(downstream_symptoms)), self._Jsonb(list(unrelated_concurrent_changes)),
                        confidence, self._Jsonb(list(unevaluated_evidence)),
                    ),
                ).fetchone()
                for evidence_id, role in evidence_links:
                    # Composite foreign keys make a cross-tenant evidence link
                    # impossible to insert, not merely discouraged.
                    self.connection.execute(
                        "INSERT INTO rca_evidence_links "
                        "(rca_id, evidence_id, role, organization_id, repository_id) "
                        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                        (rca_id, evidence_id, role, organization_id, repository_id),
                    )
        except self._psycopg.errors.UniqueViolation:
            # The transaction block already rolled back on the way out; the
            # connection is clean, so this read runs in a fresh statement.
            existing = self.connection.execute(
                "SELECT * FROM rca_reports WHERE organization_id=%s AND repository_id=%s "
                "AND incident_id=%s AND status='completed'",
                (organization_id, repository_id, incident_id),
            ).fetchone()
            return dict(existing)
        return dict(row)

    def rca_for_incident(self, organization_id, repository_id, incident_id):
        rows = self.connection.execute(
            "SELECT * FROM rca_reports WHERE organization_id=%s AND repository_id=%s "
            "AND incident_id=%s ORDER BY created_at",
            (organization_id, repository_id, incident_id),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- delivery journal --------------------------------------------------

    def record_delivery(self, organization_id, repository_id, environment, *, channel, event_key, payload, journal_id=None):
        journal_id = journal_id or str(uuid.uuid4())
        with self.connection.transaction():
            row = self.connection.execute(
                "INSERT INTO delivery_journal (journal_id, organization_id, repository_id, environment, channel, event_key, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (organization_id, repository_id, environment, channel, event_key) DO NOTHING RETURNING *",
                (journal_id, organization_id, repository_id, environment, channel, event_key, self._Jsonb(payload)),
            ).fetchone()
            if row is None:
                row = self.connection.execute(
                    "SELECT * FROM delivery_journal WHERE organization_id=%s AND repository_id=%s AND environment=%s "
                    "AND channel=%s AND event_key=%s",
                    (organization_id, repository_id, environment, channel, event_key),
                ).fetchone()
        return dict(row)

    def mark_delivered(self, organization_id, repository_id, journal_id, *, remote_id):
        self.connection.execute(
            "UPDATE delivery_journal SET status='PUBLISHED', remote_id=%s, reconciled_at=now(), "
            "attempts=attempts+1, updated_at=now() "
            "WHERE organization_id=%s AND repository_id=%s AND journal_id=%s",
            (remote_id, organization_id, repository_id, journal_id),
        )

    def mark_delivery_failed(self, organization_id, repository_id, journal_id):
        self.connection.execute(
            "UPDATE delivery_journal SET status='FAILED', attempts=attempts+1, updated_at=now() "
            "WHERE organization_id=%s AND repository_id=%s AND journal_id=%s",
            (organization_id, repository_id, journal_id),
        )

    def deliveries(self, organization_id, repository_id, environment, *, channel=None):
        if channel:
            rows = self.connection.execute(
                "SELECT * FROM delivery_journal WHERE organization_id=%s AND repository_id=%s "
                "AND environment=%s AND channel=%s",
                (organization_id, repository_id, environment, channel),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM delivery_journal WHERE organization_id=%s AND repository_id=%s AND environment=%s",
                (organization_id, repository_id, environment),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- audit --------------------------------------------------

    def append_audit(self, organization_id, repository_id, *, actor, event_type, reference_type=None, reference_id=None, payload=None):
        self.connection.execute(
            "INSERT INTO audit_events (organization_id, repository_id, actor, event_type, reference_type, reference_id, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (organization_id, repository_id, actor, event_type, reference_type, reference_id, self._Jsonb(payload or {})),
        )

    def audit_events(self, organization_id, repository_id=None):
        if repository_id:
            rows = self.connection.execute(
                "SELECT * FROM audit_events WHERE organization_id=%s AND repository_id=%s ORDER BY created_at",
                (organization_id, repository_id),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM audit_events WHERE organization_id=%s ORDER BY created_at",
                (organization_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- service tokens (public API authentication) -------------------------

    def create_service_token(self, token_id, secret_hash, organization_id, repository_id,
                             *, environment=None, description=None, expires_at=None,
                             scope="collector"):
        """Persist a token's hash. The secret itself is never stored.

        ``scope`` decides what evidence the machine token may submit. It
        defaults to ``collector`` for backwards compatibility. Governance is
        not a scope any token can hold — that requires a human session.
        """
        self.connection.execute(
            "INSERT INTO api_service_tokens "
            "(token_id, secret_hash, organization_id, repository_id, environment, "
            "description, expires_at, scope) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (token_id, secret_hash, organization_id, repository_id, environment,
             description, expires_at, scope),
        )
        return token_id

    def get_service_token(self, token_id):
        row = self.connection.execute(
            "SELECT token_id, secret_hash, organization_id, repository_id, environment, "
            "scope, expires_at, revoked_at FROM api_service_tokens WHERE token_id=%s",
            (token_id,),
        ).fetchone()
        return dict(row) if row else None

    # -- dashboard sessions ------------------------------------------------
    #
    # A session is one authenticated GitHub user holding one verified
    # repository permission. The session id is never stored: the browser holds
    # it and the server keeps only its hash, so a database disclosure cannot be
    # replayed as a live session.

    def create_dashboard_session(self, session_id_hash, *, organization_id,
                                 repository_id, environment, github_login,
                                 github_user_id, github_permission, may_govern,
                                 permission_checked_at, csrf_token, expires_at,
                                 github_access_token=None,
                                 github_access_expires_at=None,
                                 github_refresh_token=None,
                                 github_refresh_expires_at=None):
        self.connection.execute(
            "INSERT INTO dashboard_sessions ("
            "session_id_hash, organization_id, repository_id, environment, "
            "github_login, github_user_id, github_permission, may_govern, "
            "permission_checked_at, github_access_token, github_access_expires_at, "
            "github_refresh_token, github_refresh_expires_at, csrf_token, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (session_id_hash, organization_id, repository_id, environment,
             github_login, github_user_id, github_permission, may_govern,
             permission_checked_at, github_access_token, github_access_expires_at,
             github_refresh_token, github_refresh_expires_at, csrf_token, expires_at),
        )
        return session_id_hash

    def get_dashboard_session(self, session_id_hash):
        row = self.connection.execute(
            "SELECT * FROM dashboard_sessions WHERE session_id_hash=%s",
            (session_id_hash,),
        ).fetchone()
        return dict(row) if row else None

    def touch_dashboard_session(self, session_id_hash):
        self.connection.execute(
            "UPDATE dashboard_sessions SET last_seen_at=now() WHERE session_id_hash=%s",
            (session_id_hash,),
        )

    def update_dashboard_session_permission(self, session_id_hash, *,
                                            github_permission, may_govern,
                                            permission_checked_at):
        self.connection.execute(
            "UPDATE dashboard_sessions SET github_permission=%s, may_govern=%s, "
            "permission_checked_at=%s WHERE session_id_hash=%s",
            (github_permission, may_govern, permission_checked_at, session_id_hash),
        )

    def update_dashboard_session_credential(self, session_id_hash, *,
                                            github_access_token,
                                            github_access_expires_at,
                                            github_refresh_token,
                                            github_refresh_expires_at):
        """Store a rotated credential.

        GitHub issues a new refresh token every time one is used, so the old
        values must be replaced rather than kept alongside.
        """
        self.connection.execute(
            "UPDATE dashboard_sessions SET github_access_token=%s, "
            "github_access_expires_at=%s, github_refresh_token=%s, "
            "github_refresh_expires_at=%s WHERE session_id_hash=%s",
            (github_access_token, github_access_expires_at, github_refresh_token,
             github_refresh_expires_at, session_id_hash),
        )

    def revoke_dashboard_session(self, session_id_hash, reason="logout"):
        """Revoke and destroy the stored GitHub credentials in one statement.

        The credentials are cleared here rather than left to expire: a logged
        out session must not leave a usable GitHub token in the database.
        """
        row = self.connection.execute(
            "UPDATE dashboard_sessions SET revoked_at=now(), revocation_reason=%s, "
            "github_access_token=NULL, github_refresh_token=NULL "
            "WHERE session_id_hash=%s AND revoked_at IS NULL "
            "RETURNING session_id_hash",
            (reason, session_id_hash),
        ).fetchone()
        return row is not None

    def delete_expired_dashboard_sessions(self, before):
        self.connection.execute(
            "DELETE FROM dashboard_sessions WHERE expires_at < %s", (before,))

    # -- OAuth authorization states ----------------------------------------

    def create_oauth_state(self, state_hash, *, nonce_hash, redirect_to, expires_at):
        self.connection.execute(
            "INSERT INTO oauth_authorization_states "
            "(state_hash, nonce_hash, redirect_to, expires_at) VALUES (%s, %s, %s, %s)",
            (state_hash, nonce_hash, redirect_to, expires_at),
        )

    def consume_oauth_state(self, state_hash, *, now):
        """Claim a state exactly once.

        The UPDATE is the claim: two concurrent callbacks with the same state
        cannot both match ``consumed_at IS NULL``, so a replayed authorization
        loses rather than being detected afterwards.
        """
        row = self.connection.execute(
            "UPDATE oauth_authorization_states SET consumed_at=now() "
            "WHERE state_hash=%s AND consumed_at IS NULL AND expires_at > %s "
            "RETURNING state_hash, nonce_hash, redirect_to",
            (state_hash, now),
        ).fetchone()
        return dict(row) if row else None

    def delete_expired_oauth_states(self, before):
        self.connection.execute(
            "DELETE FROM oauth_authorization_states WHERE expires_at < %s", (before,))

    def list_service_tokens(self, organization_id=None, repository_id=None,
                            *, environment=None, scope=None):
        """Tokens for an operator to identify later. The hash is never returned.

        A token has to be findable after issuance - to audit it, or to revoke
        the right one - and that has to be possible without the secret, which
        is unrecoverable by design.
        """
        sql = ("SELECT token_id, organization_id, repository_id, environment, "
               "scope, description, created_at, expires_at, revoked_at "
               "FROM api_service_tokens")
        clauses, args = [], []
        if organization_id is not None:
            clauses.append("organization_id=%s")
            args.append(organization_id)
        if repository_id is not None:
            clauses.append("repository_id=%s")
            args.append(repository_id)
        if environment is not None:
            clauses.append("environment=%s")
            args.append(environment)
        if scope is not None:
            clauses.append("scope=%s")
            args.append(scope)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC"
        return [dict(r) for r in self.connection.execute(sql, tuple(args)).fetchall()]

    def revoke_service_token(self, token_id):
        """Returns True when this call performed the revocation.

        False means the token does not exist or was already revoked - the
        caller needs to know which, so it can report honestly rather than
        claim to have revoked something it did not.
        """
        row = self.connection.execute(
            "UPDATE api_service_tokens SET revoked_at=now() "
            "WHERE token_id=%s AND revoked_at IS NULL RETURNING token_id",
            (token_id,),
        ).fetchone()
        return row is not None

    def revoke_service_token_for_scope(self, organization_id, repository_id,
                                       token_id, *, scope):
        """Revoke only when the named token belongs to this exact tenant."""
        row = self.connection.execute(
            "UPDATE api_service_tokens SET revoked_at=now() "
            "WHERE token_id=%s AND organization_id=%s AND repository_id=%s "
            "AND scope=%s AND revoked_at IS NULL RETURNING token_id",
            (token_id, organization_id, repository_id, scope),
        ).fetchone()
        return row is not None

    # -- idempotent event receipts -------------------------------------------

    def get_event_receipt(self, organization_id, repository_id, event_id):
        """Idempotency keys are per tenant: the same key in two tenants is two keys."""
        row = self.connection.execute(
            "SELECT * FROM event_receipts "
            "WHERE organization_id=%s AND repository_id=%s AND event_id=%s",
            (organization_id, repository_id, event_id),
        ).fetchone()
        return dict(row) if row else None

    def record_event_receipt(self, event_id, organization_id, repository_id, environment,
                             *, status, response, payload_hash, resource_kind=None, resource_id=None):
        """Claim an idempotency key. Returns None if the key is already taken."""
        row = self.connection.execute(
            "INSERT INTO event_receipts "
            "(event_id, organization_id, repository_id, environment, status, response, "
            "payload_hash, resource_kind, resource_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (organization_id, repository_id, event_id) DO NOTHING RETURNING *",
            (event_id, organization_id, repository_id, environment, status,
             self._Jsonb(response), payload_hash, resource_kind, resource_id),
        ).fetchone()
        return dict(row) if row else None

    # -- reviews ---------------------------------------------------------------

    def create_review(self, organization_id, repository_id, environment, *, review_id,
                      decision, pull_number=None, commit_sha=None, enforcement_mode=None,
                      risk_score=None, evidence_coverage=None, payload=None):
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        row = self.connection.execute(
            "INSERT INTO reviews (review_id, organization_id, repository_id, environment, "
            "pull_number, commit_sha, decision, enforcement_mode, risk_score, evidence_coverage, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (organization_id, repository_id, review_id) DO NOTHING RETURNING *",
            (review_id, organization_id, repository_id, environment, pull_number, commit_sha,
             decision, enforcement_mode, risk_score, evidence_coverage, self._Jsonb(payload or {})),
        ).fetchone()
        if row is None:
            row = self.connection.execute(
                "SELECT * FROM reviews "
                "WHERE organization_id=%s AND repository_id=%s AND review_id=%s",
                (organization_id, repository_id, review_id),
            ).fetchone()
        return dict(row)

    def get_review(self, organization_id, repository_id, review_id):
        row = self.connection.execute(
            "SELECT * FROM reviews WHERE review_id=%s AND organization_id=%s AND repository_id=%s",
            (review_id, organization_id, repository_id),
        ).fetchone()
        return dict(row) if row else None

    def list_reviews(self, organization_id, repository_id, *, environment=None,
                     limit=25, offset=0, since=None):
        """Reviews, newest first.

        ``since`` is the plan's history window — a VISIBILITY bound, never a
        deletion. Rows outside it stay exactly where they are and come back the
        moment the workspace upgrades; a downgrade must not destroy a
        customer's history, and a retention policy that deletes is a separate
        decision from one that hides.
        """
        extra_sql, extra_params = "", ()
        if since is not None:
            extra_sql, extra_params = " AND created_at >= %s", (since,)
        return self._paged(
            "reviews", "review_id", organization_id, repository_id,
            environment=environment, limit=limit, offset=offset,
            extra_sql=extra_sql, extra_params=extra_params,
        )

    # -- tenant-scoped paginated reads ----------------------------------------

    def _paged(self, table, id_column, organization_id, repository_id, *, environment=None,
               limit=25, offset=0, extra_sql="", extra_params=()):
        """Deterministically ordered, tenant-scoped page plus a total count.

        ``table`` and ``id_column`` are internal identifiers chosen by this
        module, never caller input; all caller-supplied values are bound.
        """
        where = "organization_id=%s AND repository_id=%s"
        params = [organization_id, repository_id]
        if environment is not None:
            where += " AND environment=%s"
            params.append(environment)
        if extra_sql:
            where += extra_sql
            params.extend(extra_params)

        total = self.connection.execute(
            f"SELECT COUNT(*) AS total FROM {table} WHERE {where}", tuple(params)
        ).fetchone()["total"]
        rows = self.connection.execute(
            f"SELECT * FROM {table} WHERE {where} "
            f"ORDER BY created_at DESC, {id_column} ASC LIMIT %s OFFSET %s",
            tuple(params) + (limit, offset),
        ).fetchall()
        return {"total": total, "items": [dict(r) for r in rows]}

    def get_deployment(self, organization_id, repository_id, deployment_id):
        row = self.connection.execute(
            "SELECT * FROM deployments WHERE deployment_id=%s AND organization_id=%s AND repository_id=%s",
            (deployment_id, organization_id, repository_id),
        ).fetchone()
        return dict(row) if row else None

    def list_deployments(self, organization_id, repository_id, *, environment=None,
                         status=None, limit=25, offset=0):
        extra_sql, extra_params = ("", ())
        if status is not None:
            extra_sql, extra_params = (" AND status=%s", (status,))
        return self._paged(
            "deployments", "deployment_id", organization_id, repository_id,
            environment=environment, limit=limit, offset=offset,
            extra_sql=extra_sql, extra_params=extra_params,
        )

    def list_anomalies(self, organization_id, repository_id, *, environment=None,
                       deployment_id=None, limit=25, offset=0):
        extra_sql, extra_params = ("", ())
        if deployment_id is not None:
            extra_sql, extra_params = (" AND deployment_id=%s", (deployment_id,))
        return self._paged(
            "anomalies", "anomaly_id", organization_id, repository_id,
            environment=environment, limit=limit, offset=offset,
            extra_sql=extra_sql, extra_params=extra_params,
        )

    def get_rca(self, organization_id, repository_id, rca_id):
        row = self.connection.execute(
            "SELECT * FROM rca_reports "
            "WHERE organization_id=%s AND repository_id=%s AND rca_id=%s",
            (organization_id, repository_id, rca_id),
        ).fetchone()
        return dict(row) if row else None

    def get_anomaly(self, organization_id, repository_id, anomaly_id):
        row = self.connection.execute(
            "SELECT * FROM anomalies WHERE anomaly_id=%s AND organization_id=%s AND repository_id=%s",
            (anomaly_id, organization_id, repository_id),
        ).fetchone()
        return dict(row) if row else None

    def get_incident_scoped(self, organization_id, repository_id, incident_id):
        row = self.connection.execute(
            "SELECT * FROM incidents WHERE incident_id=%s AND organization_id=%s AND repository_id=%s",
            (incident_id, organization_id, repository_id),
        ).fetchone()
        return dict(row) if row else None

    def list_incidents(self, organization_id, repository_id, *, environment=None, limit=25, offset=0):
        return self._paged(
            "incidents", "incident_id", organization_id, repository_id,
            environment=environment, limit=limit, offset=offset,
        )

    def list_observations(self, organization_id, repository_id, *, environment=None,
                          deployment_id=None, limit=25, offset=0):
        extra_sql, extra_params = ("", ())
        if deployment_id is not None:
            extra_sql, extra_params = (" AND deployment_id=%s", (deployment_id,))
        where = "organization_id=%s AND repository_id=%s"
        params = [organization_id, repository_id]
        if environment is not None:
            where += " AND environment=%s"
            params.append(environment)
        where += extra_sql
        params.extend(extra_params)
        total = self.connection.execute(
            f"SELECT COUNT(*) AS total FROM monitoring_observations WHERE {where}", tuple(params)
        ).fetchone()["total"]
        rows = self.connection.execute(
            f"SELECT * FROM monitoring_observations WHERE {where} "
            "ORDER BY observed_at DESC, observation_id ASC LIMIT %s OFFSET %s",
            tuple(params) + (limit, offset),
        ).fetchall()
        return {"total": total, "items": [dict(r) for r in rows]}

    def lineage_for_model(self, organization_id, repository_id, model, *, environment=None):
        where = "organization_id=%s AND repository_id=%s AND model=%s"
        params = [organization_id, repository_id, model]
        if environment is not None:
            where += " AND environment=%s"
            params.append(environment)
        rows = self.connection.execute(
            f"SELECT * FROM lineage_records WHERE {where} ORDER BY created_at DESC, lineage_id ASC",
            tuple(params),
        ).fetchall()
        records = [dict(r) for r in rows]
        for record in records:
            edges = self.connection.execute(
                "SELECT upstream_model, downstream_model FROM lineage_edges "
                "WHERE organization_id=%s AND repository_id=%s AND lineage_id=%s "
                "ORDER BY upstream_model, downstream_model",
                (organization_id, repository_id, record["lineage_id"]),
            ).fetchall()
            record["edges"] = [dict(e) for e in edges]
        return records

    def kpi_impact_for_kpi(self, organization_id, repository_id, kpi_name, *, environment=None,
                           limit=25, offset=0):
        return self._paged(
            "kpi_impact", "kpi_impact_id", organization_id, repository_id,
            environment=environment, limit=limit, offset=offset,
            extra_sql=" AND kpi_name=%s", extra_params=(kpi_name,),
        )

    def repository_settings(self, organization_id, repository_id):
        environments = self.connection.execute(
            "SELECT environment, connected, created_at FROM environments "
            "WHERE organization_id=%s AND repository_id=%s ORDER BY environment",
            (organization_id, repository_id),
        ).fetchall()
        return {
            "organization_id": organization_id,
            "repository_id": repository_id,
            "environments": [dict(r) for r in environments],
        }

    def evidence_coverage(self, organization_id, repository_id, *, environment=None):
        """Coverage counts derived from stored evidence, never fabricated."""
        where = "organization_id=%s AND repository_id=%s"
        params = [organization_id, repository_id]
        if environment is not None:
            where += " AND environment=%s"
            params.append(environment)
        evidence_total = self.connection.execute(
            f"SELECT COUNT(*) AS total FROM evidence WHERE {where}", tuple(params)
        ).fetchone()["total"]
        observation_total = self.connection.execute(
            f"SELECT COUNT(*) AS total FROM monitoring_observations WHERE {where}", tuple(params)
        ).fetchone()["total"]
        baseline_total = self.connection.execute(
            f"SELECT COUNT(*) AS total FROM metadata_baselines WHERE {where}", tuple(params)
        ).fetchone()["total"]
        incomplete = self.connection.execute(
            f"SELECT COUNT(*) AS total FROM monitoring_observations "
            f"WHERE {where} AND (evidence_coverage IS NULL OR evidence_coverage <> 'COMPLETE')",
            tuple(params),
        ).fetchone()["total"]
        if observation_total == 0 and baseline_total == 0:
            state = "UNKNOWN"
        elif incomplete == 0:
            state = "COMPLETE"
        else:
            state = "INCOMPLETE"
        return {
            "coverage": state,
            "evidence_records": evidence_total,
            "observations": observation_total,
            "baselines": baseline_total,
            "observations_missing_complete_evidence": incomplete,
        }

    def monitoring_status(self, organization_id, repository_id, *, environment=None):
        where = "organization_id=%s AND repository_id=%s"
        params = [organization_id, repository_id]
        if environment is not None:
            where += " AND environment=%s"
            params.append(environment)
        latest = self.connection.execute(
            f"SELECT MAX(observed_at) AS latest FROM monitoring_observations WHERE {where}",
            tuple(params),
        ).fetchone()["latest"]
        observations = self.connection.execute(
            f"SELECT COUNT(*) AS total FROM monitoring_observations WHERE {where}", tuple(params)
        ).fetchone()["total"]
        open_anomalies = self.connection.execute(
            f"SELECT COUNT(*) AS total FROM anomalies WHERE {where}", tuple(params)
        ).fetchone()["total"]
        open_incidents = self.connection.execute(
            f"SELECT COUNT(*) AS total FROM incidents WHERE {where} AND status <> 'resolved'",
            tuple(params),
        ).fetchone()["total"]
        coverage = self.evidence_coverage(organization_id, repository_id, environment=environment)
        # Missing evidence degrades coverage, never health.
        if open_incidents > 0:
            health = "DEGRADED"
        elif open_anomalies > 0:
            health = "ANOMALOUS"
        elif observations > 0:
            health = "HEALTHY"
        else:
            health = "UNKNOWN"
        return {
            "health": health,
            "observations": observations,
            "anomalies": open_anomalies,
            "unresolved_incidents": open_incidents,
            "latest_observation_at": latest,
            "evidence_coverage": coverage["coverage"],
        }

    def outbox_stats(self, organization_id=None, repository_id=None):
        if organization_id and repository_id:
            rows = self.connection.execute(
                "SELECT state, COUNT(*) AS total FROM outbox_events "
                "WHERE organization_id=%s AND repository_id=%s GROUP BY state",
                (organization_id, repository_id),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT state, COUNT(*) AS total FROM outbox_events GROUP BY state"
            ).fetchall()
        return {row["state"]: row["total"] for row in rows}

    # =================================================================
    # Metadata evidence plane
    #
    # Production metadata is a first-class pre-deployment input. Everything
    # below is tenant scoped; a snapshot is immutable once accepted and a
    # correction requires a new snapshot.
    # =================================================================

    REVIEW_LIFECYCLE_STATES = (
        "RECEIVED",
        "WAITING_FOR_MANIFEST",
        "CODE_ANALYSIS_COMPLETE",
        "METADATA_NOT_REQUIRED",
        "METADATA_REQUESTED",
        "WAITING_FOR_METADATA",
        "METADATA_PARTIAL",
        "METADATA_COMPLETE",
        "METADATA_STALE",
        "DECISION_READY",
        "PUBLISHED",
        "FAILED",
    )

    # -- immutable CI manifest evidence ----------------------------------

    def submit_manifest_evidence(self, organization_id, repository_id, *,
                                 commit_sha, manifest, manifest_hash,
                                 idempotency_key, payload_hash):
        """Insert one immutable manifest for an exact commit SHA.

        Both uniqueness decisions happen inside one PostgreSQL transaction.
        ``INSERT .. ON CONFLICT`` serializes concurrent submitters; the loser
        then reads the committed winner and can distinguish an identical retry
        from a conflicting replay without leaking another tenant's row.
        """
        evidence_id = f"manifest-{uuid.uuid4().hex[:24]}"
        with self.connection.transaction():
            # Serialize evidence arrivals per repository. Without this lock,
            # simultaneous BASE and HEAD transactions can each take a
            # statement snapshot before the other commits and both miss the
            # now-ready pair, leaving a waiting review without resume work.
            tenant = self.connection.execute(
                "SELECT 1 FROM repositories WHERE organization_id=%s "
                "AND repository_id=%s FOR UPDATE",
                (organization_id, repository_id),
            ).fetchone()
            if tenant is None:
                raise RuntimeError("manifest evidence repository does not exist")
            row = self.connection.execute(
                "INSERT INTO manifest_evidence (organization_id, repository_id, "
                "evidence_id, commit_sha, manifest_hash, manifest, idempotency_key, "
                "payload_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING RETURNING *",
                (organization_id, repository_id, evidence_id, commit_sha,
                 manifest_hash, self._Jsonb(manifest), idempotency_key,
                 payload_hash),
            ).fetchone()

            created = row is not None
            if row is None:
                by_key = self.connection.execute(
                    "SELECT * FROM manifest_evidence WHERE organization_id=%s "
                    "AND repository_id=%s AND idempotency_key=%s",
                    (organization_id, repository_id, idempotency_key),
                ).fetchone()
                if by_key is not None:
                    if (by_key["payload_hash"] == payload_hash
                            and by_key["commit_sha"] == commit_sha):
                        row = by_key
                    else:
                        raise ManifestEvidenceConflict(
                            "idempotency key already used with different manifest evidence")
                else:
                    by_sha = self.connection.execute(
                        "SELECT * FROM manifest_evidence WHERE organization_id=%s "
                        "AND repository_id=%s AND commit_sha=%s",
                        (organization_id, repository_id, commit_sha),
                    ).fetchone()
                    if by_sha is not None and by_sha["manifest_hash"] == manifest_hash:
                        row = by_sha
                    elif by_sha is not None:
                        raise ManifestEvidenceConflict(
                            "commit SHA already has different manifest evidence")
                    else:
                        # The insert can only lose to one of the two unique keys. If
                        # neither scoped row is visible, surface a real persistence
                        # error instead of pretending the request was accepted.
                        raise RuntimeError(
                            "manifest evidence conflict could not be reconciled")

            waiting = self.connection.execute(
                "SELECT r.review_id, r.environment, r.base_sha, r.head_sha, "
                "base_evidence.evidence_id AS base_evidence_id, "
                "head_evidence.evidence_id AS head_evidence_id "
                "FROM reviews r "
                "JOIN manifest_evidence base_evidence "
                "ON base_evidence.organization_id=r.organization_id "
                "AND base_evidence.repository_id=r.repository_id "
                "AND base_evidence.commit_sha=r.base_sha "
                "JOIN manifest_evidence head_evidence "
                "ON head_evidence.organization_id=r.organization_id "
                "AND head_evidence.repository_id=r.repository_id "
                "AND head_evidence.commit_sha=r.head_sha "
                "WHERE r.organization_id=%s AND r.repository_id=%s "
                "AND (r.base_sha=%s OR r.head_sha=%s) "
                "AND r.lifecycle_state='WAITING_FOR_MANIFEST' "
                "FOR UPDATE OF r",
                (organization_id, repository_id, commit_sha, commit_sha),
            ).fetchall()
            for review in waiting:
                pair_key = f"{review['base_sha']}:{review['head_sha']}"
                self.connection.execute(
                    "INSERT INTO outbox_events (event_id, organization_id, "
                    "repository_id, environment, subject_type, subject_id, "
                    "deployment_id, event_type, payload, dedup_key) "
                    "VALUES (%s, %s, %s, %s, 'review', %s, NULL, "
                    "'review.manifest_resume_requested', %s, %s) "
                    "ON CONFLICT (organization_id, repository_id, environment, "
                    "subject_type, subject_id, event_type, dedup_key) DO NOTHING",
                    (str(uuid.uuid4()), organization_id, repository_id,
                     review["environment"], review["review_id"],
                     self._Jsonb({"review_id": review["review_id"],
                                  "commit_sha": review["head_sha"],
                                  "base_evidence_id": review["base_evidence_id"],
                                  "head_evidence_id": review["head_evidence_id"]}),
                     pair_key),
                )
        return dict(row), created

    def get_manifest_evidence(self, organization_id, repository_id, commit_sha):
        """Return evidence only for the tenant, repository and exact SHA."""
        row = self.connection.execute(
            "SELECT * FROM manifest_evidence WHERE organization_id=%s "
            "AND repository_id=%s AND commit_sha=%s",
            (organization_id, repository_id, commit_sha),
        ).fetchone()
        return dict(row) if row else None

    # -- reviews -----------------------------------------------------------

    def upsert_pr_review(self, organization_id, repository_id, environment, *, review_id,
                         pull_number=None, base_sha=None, head_sha=None,
                         base_manifest_hash=None, head_manifest_hash=None,
                         enforcement_mode=None, policy_version=None, policy_hash=None,
                         github_delivery_id=None, metadata_required=False,
                         lifecycle_state="RECEIVED", payload=None):
        """Persist a review from the live GitHub path.

        This is the authoritative review record. It is created before any
        decision exists, so ``decision`` stays NULL until one is reached.
        """
        if lifecycle_state not in self.REVIEW_LIFECYCLE_STATES:
            raise ValueError(f"unknown review lifecycle state: {lifecycle_state}")
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        with self.connection.transaction():
            row = self.connection.execute(
                "INSERT INTO reviews (review_id, organization_id, repository_id, environment, "
                "pull_number, commit_sha, decision, enforcement_mode, evidence_coverage, "
                "lifecycle_state, base_sha, head_sha, base_manifest_hash, head_manifest_hash, "
                "policy_version, policy_hash, github_delivery_id, metadata_required, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, 'UNKNOWN', %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s) "
                "ON CONFLICT (organization_id, repository_id, review_id) DO NOTHING "
                "RETURNING *",
                (review_id, organization_id, repository_id, environment, pull_number,
                 head_sha, enforcement_mode, lifecycle_state, base_sha, head_sha,
                 base_manifest_hash, head_manifest_hash, policy_version, policy_hash,
                 github_delivery_id, bool(metadata_required), self._Jsonb(payload or {})),
            ).fetchone()
            if row is None:
                row = self.connection.execute(
                    "SELECT * FROM reviews WHERE organization_id=%s AND repository_id=%s "
                    "AND review_id=%s",
                    (organization_id, repository_id, review_id),
                ).fetchone()
                # A webhook may have created the stable review identity before
                # CI delivered its manifest. Once that exact artifact arrives,
                # fill in the analysis binding on the SAME review; do not make
                # a second review or lose the sticky GitHub publication IDs.
                if (row is not None
                        and row["lifecycle_state"] == "WAITING_FOR_MANIFEST"
                        and lifecycle_state != "WAITING_FOR_MANIFEST"):
                    row = self.connection.execute(
                        "UPDATE reviews SET base_manifest_hash=%s, "
                        "head_manifest_hash=%s, enforcement_mode=%s, "
                        "policy_version=%s, policy_hash=%s, "
                        "metadata_required=%s, payload=%s, updated_at=now() "
                        "WHERE organization_id=%s AND repository_id=%s "
                        "AND review_id=%s RETURNING *",
                        (base_manifest_hash, head_manifest_hash,
                         enforcement_mode, policy_version, policy_hash,
                         bool(metadata_required), self._Jsonb(payload or {}),
                         organization_id, repository_id, review_id),
                    ).fetchone()
            else:
                self.connection.execute(
                    "INSERT INTO review_lifecycle_transitions "
                    "(organization_id, repository_id, review_id, from_state, to_state, reason) "
                    "VALUES (%s, %s, %s, NULL, %s, %s)",
                    (organization_id, repository_id, review_id, lifecycle_state,
                     "review received"),
                )
        return dict(row)

    def transition_review(self, organization_id, repository_id, review_id, to_state, *,
                          reason=None):
        """Move the review lifecycle forward. Idempotent: re-entering the same
        state is a no-op and does not append a duplicate transition."""
        if to_state not in self.REVIEW_LIFECYCLE_STATES:
            raise ValueError(f"unknown review lifecycle state: {to_state}")
        with self.connection.transaction():
            current = self.connection.execute(
                "SELECT lifecycle_state FROM reviews "
                "WHERE organization_id=%s AND repository_id=%s AND review_id=%s FOR UPDATE",
                (organization_id, repository_id, review_id),
            ).fetchone()
            if current is None:
                raise LookupError("review not found")
            from_state = current["lifecycle_state"]
            if from_state == to_state:
                return {"review_id": review_id, "lifecycle_state": to_state,
                        "transition_applied": False}
            self.connection.execute(
                "UPDATE reviews SET lifecycle_state=%s, updated_at=now() "
                "WHERE organization_id=%s AND repository_id=%s AND review_id=%s",
                (to_state, organization_id, repository_id, review_id),
            )
            self.connection.execute(
                "INSERT INTO review_lifecycle_transitions "
                "(organization_id, repository_id, review_id, from_state, to_state, reason) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (organization_id, repository_id, review_id, from_state, to_state, reason),
            )
        return {"review_id": review_id, "lifecycle_state": to_state,
                "from_state": from_state, "transition_applied": True}

    def review_transitions(self, organization_id, repository_id, review_id):
        rows = self.connection.execute(
            "SELECT * FROM review_lifecycle_transitions "
            "WHERE organization_id=%s AND repository_id=%s AND review_id=%s "
            "ORDER BY transition_id",
            (organization_id, repository_id, review_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def record_review_decision(self, organization_id, repository_id, review_id, *,
                               decision, evidence_coverage, health, attempt,
                               trigger="initial", snapshot_id=None,
                               enforcement_mode=None, policy_version=None,
                               policy_hash=None, payload=None,
                               semantic_evidence=None, metadata_comparison=None):
        """Record a decision and preserve it as an immutable attempt.

        Coverage, health, decision and lifecycle state are stored separately;
        none of them is derived from another at read time.
        """
        with self.connection.transaction():
            self.connection.execute(
                "UPDATE reviews SET decision=%s, evidence_coverage=%s, health=%s, "
                "attempt=%s, enforcement_mode=COALESCE(%s, enforcement_mode), "
                "policy_version=COALESCE(%s, policy_version), "
                "policy_hash=COALESCE(%s, policy_hash), updated_at=now() "
                "WHERE organization_id=%s AND repository_id=%s AND review_id=%s",
                (decision, evidence_coverage, health, attempt, enforcement_mode,
                 policy_version, policy_hash, organization_id, repository_id, review_id),
            )
            self.connection.execute(
                "INSERT INTO review_attempts (organization_id, repository_id, review_id, "
                "attempt, lifecycle_state, decision, evidence_coverage, health, "
                "enforcement_mode, policy_version, policy_hash, trigger, snapshot_id, "
                "payload, semantic_evidence, metadata_comparison) "
                "SELECT %s, %s, %s, %s, lifecycle_state, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s "
                "FROM reviews WHERE organization_id=%s AND repository_id=%s AND review_id=%s "
                "ON CONFLICT (organization_id, repository_id, review_id, attempt) DO NOTHING",
                (organization_id, repository_id, review_id, attempt, decision,
                 evidence_coverage, health, enforcement_mode, policy_version, policy_hash,
                 trigger, snapshot_id, self._Jsonb(payload or {}),
                 # NULL when no comparison ran: an absent row must never read
                 # as a clean comparison.
                 self._Jsonb(semantic_evidence) if semantic_evidence is not None else None,
                 # Same rule for the production metadata comparison. NULL is
                 # "never computed"; status=no_baseline is "computed, and there
                 # was no prior observation to compare against".
                 self._Jsonb(metadata_comparison) if metadata_comparison is not None else None,
                 organization_id, repository_id, review_id),
            )
        return self.get_review(organization_id, repository_id, review_id)

    def review_attempts(self, organization_id, repository_id, review_id):
        rows = self.connection.execute(
            "SELECT * FROM review_attempts "
            "WHERE organization_id=%s AND repository_id=%s AND review_id=%s ORDER BY attempt",
            (organization_id, repository_id, review_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def collection_requests_for_review(self, organization_id, repository_id, review_id):
        """Every collection request raised for one review, in creation order.

        ``pending_collection_requests`` answers the collector's question - what
        should I work on. This answers the dashboard's - what was asked for and
        what happened to it - so completed, failed and expired requests are
        included rather than filtered out.
        """
        rows = self.connection.execute(
            "SELECT * FROM collection_requests "
            "WHERE organization_id=%s AND repository_id=%s AND review_id=%s "
            "ORDER BY created_at, request_id",
            (organization_id, repository_id, review_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def snapshots_for_review(self, organization_id, repository_id, review_id):
        """Every snapshot submitted against one review, newest first."""
        rows = self.connection.execute(
            "SELECT snapshot_id, environment, completeness, freshness_state, "
            "observed_at, received_at, collected_at, request_id, collector_id, "
            "collector_version, adapter_type, base_sha, head_sha, "
            "base_manifest_hash, head_manifest_hash, ttl_seconds "
            "FROM metadata_snapshots "
            "WHERE organization_id=%s AND repository_id=%s AND review_id=%s "
            "ORDER BY received_at DESC, snapshot_id",
            (organization_id, repository_id, review_id),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- human governance actions -----------------------------------------
    #
    # Neither of these writes to `reviews`. What Relium decided and what a
    # person decided about it are separate facts, and they stay separate.

    def create_change_request(self, organization_id, repository_id, environment, *,
                              change_request_id, review_id, attempt, pull_number,
                              head_sha, actor, message):
        """Record the intent to submit a GitHub request-changes review.

        Returns (row, created). A second call for the same (review, attempt)
        returns the existing row with created=False, so a double click cannot
        submit two GitHub reviews on one pull request.
        """
        with self.connection.transaction():
            row = self.connection.execute(
                "INSERT INTO review_change_requests (organization_id, repository_id, "
                "change_request_id, review_id, attempt, environment, pull_number, "
                "head_sha, actor, message) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING RETURNING *",
                (organization_id, repository_id, change_request_id, review_id,
                 attempt, environment, pull_number, head_sha, actor, message),
            ).fetchone()
            if row is not None:
                return dict(row), True
            existing = self.connection.execute(
                "SELECT * FROM review_change_requests WHERE organization_id=%s "
                "AND repository_id=%s AND review_id=%s AND attempt=%s "
                "AND state IN ('PENDING','PUBLISHED')",
                (organization_id, repository_id, review_id, attempt),
            ).fetchone()
        return (dict(existing) if existing else None), False

    def complete_change_request(self, organization_id, repository_id,
                                change_request_id, *, remote_review_id=None,
                                failure_reason=None):
        """Mark a change request published or failed. Never both."""
        state = "FAILED" if failure_reason else "PUBLISHED"
        normalized_remote_id = (
            None if failure_reason else normalize_remote_review_id(remote_review_id)
        )
        self.connection.execute(
            "UPDATE review_change_requests SET state=%s, remote_review_id=%s, "
            "failure_reason=%s, published_at=CASE WHEN %s='PUBLISHED' THEN now() END "
            "WHERE organization_id=%s AND repository_id=%s AND change_request_id=%s",
            (state, normalized_remote_id,
             failure_reason, state, organization_id, repository_id, change_request_id),
        )
        return self.get_change_request(organization_id, repository_id, change_request_id)

    def get_change_request(self, organization_id, repository_id, change_request_id):
        row = self.connection.execute(
            "SELECT * FROM review_change_requests WHERE organization_id=%s "
            "AND repository_id=%s AND change_request_id=%s",
            (organization_id, repository_id, change_request_id),
        ).fetchone()
        return dict(row) if row else None

    def change_requests_for_review(self, organization_id, repository_id, review_id):
        rows = self.connection.execute(
            "SELECT * FROM review_change_requests WHERE organization_id=%s "
            "AND repository_id=%s AND review_id=%s ORDER BY created_at",
            (organization_id, repository_id, review_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def create_review_exception(self, organization_id, repository_id, environment, *,
                                exception_id, review_id, attempt, actor, reason,
                                scope="attempt", overridden_decision=None,
                                base_sha=None, head_sha=None):
        """Approve an exception against one attempt's decision.

        Returns (row, created). The review's own decision is never written.
        """
        with self.connection.transaction():
            row = self.connection.execute(
                "INSERT INTO review_exceptions (organization_id, repository_id, "
                "exception_id, review_id, attempt, environment, overridden_decision, "
                "base_sha, head_sha, actor, reason, scope) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING RETURNING *",
                (organization_id, repository_id, exception_id, review_id, attempt,
                 environment, overridden_decision, base_sha, head_sha, actor,
                 reason, scope),
            ).fetchone()
            if row is not None:
                return dict(row), True
            existing = self.connection.execute(
                "SELECT * FROM review_exceptions WHERE organization_id=%s "
                "AND repository_id=%s AND review_id=%s AND attempt=%s AND state='active'",
                (organization_id, repository_id, review_id, attempt),
            ).fetchone()
        return (dict(existing) if existing else None), False

    def revoke_review_exception(self, organization_id, repository_id, exception_id, *,
                                actor, reason):
        self.connection.execute(
            "UPDATE review_exceptions SET state='revoked', revoked_at=now(), "
            "revoked_by=%s, revocation_reason=%s "
            "WHERE organization_id=%s AND repository_id=%s AND exception_id=%s "
            "AND state='active'",
            (actor, reason, organization_id, repository_id, exception_id),
        )
        return self.get_review_exception(organization_id, repository_id, exception_id)

    def get_review_exception(self, organization_id, repository_id, exception_id):
        row = self.connection.execute(
            "SELECT * FROM review_exceptions WHERE organization_id=%s "
            "AND repository_id=%s AND exception_id=%s",
            (organization_id, repository_id, exception_id),
        ).fetchone()
        return dict(row) if row else None

    def exceptions_for_review(self, organization_id, repository_id, review_id):
        """Every exception ever approved for this review, newest first."""
        rows = self.connection.execute(
            "SELECT * FROM review_exceptions WHERE organization_id=%s "
            "AND repository_id=%s AND review_id=%s ORDER BY created_at DESC",
            (organization_id, repository_id, review_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def active_exception_for_attempt(self, organization_id, repository_id, review_id,
                                     attempt):
        """The exception in force for exactly this attempt, if any.

        Attempt-scoped by default: a later attempt analysed newer evidence, and
        must not inherit an override approved against findings nobody has
        re-examined. A 'review'-scoped exception is honoured across attempts
        because that scope was chosen explicitly.
        """
        row = self.connection.execute(
            "SELECT * FROM review_exceptions WHERE organization_id=%s "
            "AND repository_id=%s AND review_id=%s AND state='active' "
            "AND (attempt=%s OR scope='review') "
            "ORDER BY (attempt=%s) DESC, created_at DESC LIMIT 1",
            (organization_id, repository_id, review_id, attempt, attempt),
        ).fetchone()
        return dict(row) if row else None

    def record_review_publication(self, organization_id, repository_id, review_id, *,
                                  comment_id=None, check_run_id=None):
        """Remember the sticky comment and check run so a recomputation updates
        the same GitHub objects instead of publishing duplicates."""
        self.connection.execute(
            "UPDATE reviews SET github_comment_id=COALESCE(%s, github_comment_id), "
            "github_check_run_id=COALESCE(%s, github_check_run_id), updated_at=now() "
            "WHERE organization_id=%s AND repository_id=%s AND review_id=%s",
            (str(comment_id) if comment_id is not None else None,
             str(check_run_id) if check_run_id is not None else None,
             organization_id, repository_id, review_id),
        )
        return self.get_review(organization_id, repository_id, review_id)

    def record_evidence_states(self, organization_id, repository_id, review_id, attempt,
                               states):
        """Persist one row per evidence source for this attempt.

        ``states`` maps source -> (requirement, state, group, detail).
        """
        with self.connection.transaction():
            for source, entry in states.items():
                requirement, state, group, detail = entry
                self.connection.execute(
                    "INSERT INTO review_evidence_coverage (organization_id, repository_id, "
                    "review_id, attempt, evidence_source, requirement, state, "
                    "evidence_state_group, detail) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (organization_id, repository_id, review_id, attempt, "
                    "evidence_source) DO UPDATE SET state=EXCLUDED.state, "
                    "requirement=EXCLUDED.requirement, detail=EXCLUDED.detail",
                    (organization_id, repository_id, review_id, attempt, source,
                     requirement, state, group, detail),
                )
        return self.evidence_states(organization_id, repository_id, review_id, attempt)

    def evidence_states(self, organization_id, repository_id, review_id, attempt=None):
        if attempt is None:
            rows = self.connection.execute(
                "SELECT * FROM review_evidence_coverage WHERE organization_id=%s "
                "AND repository_id=%s AND review_id=%s ORDER BY attempt, evidence_source",
                (organization_id, repository_id, review_id),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM review_evidence_coverage WHERE organization_id=%s "
                "AND repository_id=%s AND review_id=%s AND attempt=%s ORDER BY evidence_source",
                (organization_id, repository_id, review_id, attempt),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- collector identity ------------------------------------------------

    def register_collector(self, organization_id, repository_id, environment, *,
                           collector_id, token_id=None, collector_version=None,
                           adapter_type=None, description=None):
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        row = self.connection.execute(
            "INSERT INTO collector_identities (organization_id, repository_id, collector_id, "
            "environment, token_id, collector_version, adapter_type, description) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (organization_id, repository_id, collector_id) DO UPDATE SET "
            "token_id=EXCLUDED.token_id, "
            "collector_version=EXCLUDED.collector_version, "
            "adapter_type=EXCLUDED.adapter_type, description=COALESCE("
            "EXCLUDED.description, collector_identities.description), "
            "last_seen_at=now(), revoked=FALSE, revoked_at=NULL, "
            "revoked_reason=NULL RETURNING *",
            (organization_id, repository_id, collector_id, environment, token_id,
             collector_version, adapter_type, description),
        ).fetchone()
        return dict(row)

    def list_collectors(self, organization_id, repository_id, *, environment=None):
        sql = ("SELECT * FROM collector_identities WHERE organization_id=%s "
               "AND repository_id=%s AND revoked=FALSE")
        args = [organization_id, repository_id]
        if environment is not None:
            sql += " AND environment=%s"
            args.append(environment)
        sql += " ORDER BY last_seen_at DESC NULLS LAST, created_at DESC"
        return [dict(row) for row in self.connection.execute(sql, tuple(args)).fetchall()]

    def record_collector_verification(self, organization_id, repository_id,
                                      environment, *, collector_id, token_id,
                                      status, adapter_type,
                                      error_category=None):
        if status not in ("verified", "failed"):
            raise ValueError("invalid collector verification status")
        timestamp_column = ("last_verified_at" if status == "verified"
                            else "last_failed_at")
        row = self.connection.execute(
            f"UPDATE collector_identities SET verification_status=%s, "
            f"verification_error_category=%s, {timestamp_column}=now(), "
            "last_seen_at=now() WHERE organization_id=%s AND repository_id=%s "
            "AND environment=%s AND collector_id=%s AND token_id=%s "
            "AND adapter_type=%s AND revoked=FALSE RETURNING *",
            (status, error_category, organization_id, repository_id,
             environment, collector_id, token_id, adapter_type),
        ).fetchone()
        return dict(row) if row else None

    def waiting_metadata_reviews(self, organization_id, repository_id, *,
                                 environment):
        rows = self.connection.execute(
            "SELECT r.review_id, r.pull_number, r.lifecycle_state, "
            "cr.request_id, cr.expires_at FROM reviews r "
            "LEFT JOIN LATERAL (SELECT request_id, expires_at FROM "
            "collection_requests WHERE organization_id=r.organization_id "
            "AND repository_id=r.repository_id AND review_id=r.review_id "
            "AND state IN ('PENDING','ACKNOWLEDGED') ORDER BY created_at DESC "
            "LIMIT 1) cr ON TRUE WHERE r.organization_id=%s "
            "AND r.repository_id=%s AND r.environment=%s "
            "AND r.lifecycle_state IN ('METADATA_REQUESTED','WAITING_FOR_METADATA') "
            "ORDER BY r.updated_at DESC",
            (organization_id, repository_id, environment),
        ).fetchall()
        return [dict(row) for row in rows]

    def revoke_collector(self, organization_id, repository_id, collector_id, *, reason=None):
        self.connection.execute(
            "UPDATE collector_identities SET revoked=TRUE, revoked_at=now(), "
            "revoked_reason=%s WHERE organization_id=%s AND repository_id=%s "
            "AND collector_id=%s AND revoked=FALSE",
            (reason, organization_id, repository_id, collector_id),
        )
        return self.get_collector(organization_id, repository_id, collector_id)

    def get_collector(self, organization_id, repository_id, collector_id):
        row = self.connection.execute(
            "SELECT * FROM collector_identities WHERE organization_id=%s "
            "AND repository_id=%s AND collector_id=%s",
            (organization_id, repository_id, collector_id),
        ).fetchone()
        return dict(row) if row else None

    # -- targeted collection requests --------------------------------------

    def create_collection_request(self, organization_id, repository_id, environment, *,
                                  request_id, reason, expires_at, targets,
                                  review_id=None, deployment_id=None, base_sha=None,
                                  head_sha=None, base_manifest_hash=None,
                                  head_manifest_hash=None, priority="standard",
                                  required_evidence_level="schema", plan=None):
        """Create a bounded targeted collection request.

        ``targets`` is the explicit relation list. An empty target list is
        rejected: a request that names nothing would invite a full warehouse
        scan, which this design forbids.
        """
        targets = list(targets or [])
        if not targets:
            raise ValueError("a collection request must name at least one target relation")
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        with self.connection.transaction():
            row = self.connection.execute(
                "INSERT INTO collection_requests (organization_id, repository_id, request_id, "
                "environment, review_id, deployment_id, base_sha, head_sha, "
                "base_manifest_hash, head_manifest_hash, reason, priority, "
                "required_evidence_level, expires_at, plan) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (organization_id, repository_id, request_id) DO NOTHING "
                "RETURNING *",
                (organization_id, repository_id, request_id, environment, review_id,
                 deployment_id, base_sha, head_sha, base_manifest_hash, head_manifest_hash,
                 reason, priority, required_evidence_level, expires_at,
                 self._Jsonb(plan or {})),
            ).fetchone()
            if row is None:
                return self.get_collection_request(organization_id, repository_id, request_id)
            for index, target in enumerate(targets):
                self.connection.execute(
                    "INSERT INTO collection_request_targets (organization_id, repository_id, "
                    "request_id, target_index, model_unique_id, relation_database, "
                    "relation_schema, relation_name, columns, required_signals, "
                    "dependency_kind, criticality) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (organization_id, repository_id, request_id, index,
                     target.get("model_unique_id"), target.get("relation_database"),
                     target.get("relation_schema"), target["relation_name"],
                     self._Jsonb(list(target.get("columns") or [])),
                     self._Jsonb(list(target.get("required_signals") or [])),
                     target.get("dependency_kind", "external"),
                     target.get("criticality", "standard")),
                )
        return self.get_collection_request(organization_id, repository_id, request_id)

    def get_collection_request(self, organization_id, repository_id, request_id):
        row = self.connection.execute(
            "SELECT * FROM collection_requests WHERE organization_id=%s "
            "AND repository_id=%s AND request_id=%s",
            (organization_id, repository_id, request_id),
        ).fetchone()
        if row is None:
            return None
        request = dict(row)
        request["targets"] = [dict(t) for t in self.connection.execute(
            "SELECT * FROM collection_request_targets WHERE organization_id=%s "
            "AND repository_id=%s AND request_id=%s ORDER BY target_index",
            (organization_id, repository_id, request_id),
        ).fetchall()]
        return request

    def pending_collection_requests(self, organization_id, repository_id, *,
                                    environment=None, limit=10):
        """Requests a collector may work on. Expired requests are excluded and
        marked, so a collector never acts on a stale plan."""
        self.reconcile_terminal_collection_requests(
            organization_id, repository_id, environment=environment)
        sql = ("SELECT request_id FROM collection_requests WHERE organization_id=%s "
               "AND repository_id=%s AND state='PENDING' AND expires_at > now()")
        args = [organization_id, repository_id]
        if environment is not None:
            sql += " AND environment=%s"
            args.append(environment)
        sql += (" ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'standard' THEN 1 "
                "ELSE 2 END, created_at LIMIT %s")
        args.append(int(limit))
        rows = self.connection.execute(sql, tuple(args)).fetchall()
        return [self.get_collection_request(organization_id, repository_id, r["request_id"])
                for r in rows]

    def reconcile_terminal_collection_requests(self, organization_id, repository_id, *,
                                               environment=None):
        """Expire stale requests and reconcile the review projection.

        A refresh request temporarily moves an already-decided review to
        ``METADATA_REQUESTED``. If that refresh ends without evidence, the
        immutable prior attempt remains authoritative: only the review's
        lifecycle projection is restored. No attempt or publication job is
        created here.

        A never-decided review has no prior decision to restore. Once its
        request is terminal it returns to ``WAITING_FOR_METADATA`` so a new
        request can be issued instead of remaining stranded in a state that
        implies an actionable request still exists.

        The newest request must be unsuccessful and no actionable request may
        remain. This prevents an old expired request from reconciling away a
        newer live refresh, and prevents the short COMPLETED -> recomputation
        interval from restoring the old decision.
        """
        env_sql = " AND environment=%s" if environment is not None else ""
        scope_args = [organization_id, repository_id]
        if environment is not None:
            scope_args.append(environment)

        with self.connection.transaction():
            expired = self.connection.execute(
                "UPDATE collection_requests SET state='EXPIRED' "
                "WHERE organization_id=%s AND repository_id=%s "
                "AND state IN ('PENDING','ACKNOWLEDGED') AND expires_at <= now()"
                + env_sql + " RETURNING request_id, review_id",
                tuple(scope_args),
            ).fetchall()

            for request in expired:
                self.connection.execute(
                    "INSERT INTO audit_events "
                    "(organization_id, repository_id, actor, event_type, "
                    "reference_type, reference_id, payload) "
                    "VALUES (%s, %s, 'worker:lifecycle', 'collection.expired', "
                    "'collection_request', %s, %s)",
                    (organization_id, repository_id, request["request_id"],
                     self._Jsonb({"review_id": request["review_id"]})),
                )

            candidates = self.connection.execute(
                "SELECT r.review_id, r.decision, latest.request_id, latest.state "
                "FROM reviews r "
                "JOIN LATERAL ("
                "  SELECT cr.request_id, cr.state FROM collection_requests cr "
                "  WHERE cr.organization_id=r.organization_id "
                "    AND cr.repository_id=r.repository_id "
                "    AND cr.review_id=r.review_id "
                "  ORDER BY cr.created_at DESC, cr.request_id DESC LIMIT 1"
                ") latest ON TRUE "
                "WHERE r.organization_id=%s AND r.repository_id=%s "
                "AND r.lifecycle_state='METADATA_REQUESTED' "
                "AND latest.state IN ('EXPIRED','FAILED') "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM collection_requests active "
                "  WHERE active.organization_id=r.organization_id "
                "    AND active.repository_id=r.repository_id "
                "    AND active.review_id=r.review_id "
                "    AND active.state IN ('PENDING','ACKNOWLEDGED') "
                "    AND active.expires_at > now()"
                ")" + env_sql.replace("environment", "r.environment") +
                " FOR UPDATE OF r",
                tuple(scope_args),
            ).fetchall()

            restored = []
            waiting = []
            for candidate in candidates:
                target_state = (
                    "DECISION_READY" if candidate.get("decision") is not None
                    else "WAITING_FOR_METADATA"
                )
                reason = (
                    "metadata refresh ended without new evidence"
                    if target_state == "DECISION_READY"
                    else "metadata request ended without evidence"
                )
                self.connection.execute(
                    "UPDATE reviews SET lifecycle_state=%s, updated_at=now() "
                    "WHERE organization_id=%s AND repository_id=%s AND review_id=%s "
                    "AND lifecycle_state='METADATA_REQUESTED'",
                    (target_state, organization_id, repository_id,
                     candidate["review_id"]),
                )
                self.connection.execute(
                    "INSERT INTO review_lifecycle_transitions "
                    "(organization_id, repository_id, review_id, from_state, "
                    "to_state, reason) VALUES (%s, %s, %s, 'METADATA_REQUESTED', %s, %s)",
                    (organization_id, repository_id, candidate["review_id"],
                     target_state, reason),
                )
                self.connection.execute(
                    "INSERT INTO audit_events "
                    "(organization_id, repository_id, actor, event_type, "
                    "reference_type, reference_id, payload) "
                    "VALUES (%s, %s, 'worker:lifecycle', "
                    "'review.metadata_request_reconciled', 'review', %s, %s)",
                    (organization_id, repository_id, candidate["review_id"],
                     self._Jsonb({
                         "request_id": candidate["request_id"],
                         "request_state": candidate["state"],
                          "restored_lifecycle_state": target_state,
                      })),
                )
                if target_state == "DECISION_READY":
                    restored.append(candidate["review_id"])
                else:
                    waiting.append(candidate["review_id"])

        return {
            "expired_request_ids": [row["request_id"] for row in expired],
            "restored_review_ids": restored,
            "waiting_review_ids": waiting,
        }

    def acknowledge_collection_request(self, organization_id, repository_id, request_id, *,
                                       collector_id):
        row = self.connection.execute(
            "UPDATE collection_requests SET state='ACKNOWLEDGED', acknowledged_by=%s, "
            "acknowledged_at=now() WHERE organization_id=%s AND repository_id=%s "
            "AND request_id=%s AND state='PENDING' AND expires_at > now() RETURNING *",
            (collector_id, organization_id, repository_id, request_id),
        ).fetchone()
        return dict(row) if row else None

    def close_collection_request(self, organization_id, repository_id, request_id, *,
                                 state, failure_reason=None):
        if state not in ("COMPLETED", "PARTIAL", "FAILED", "EXPIRED"):
            raise ValueError(f"invalid terminal collection request state: {state}")
        self.connection.execute(
            "UPDATE collection_requests SET state=%s, failure_reason=%s, completed_at=now() "
            "WHERE organization_id=%s AND repository_id=%s AND request_id=%s "
            "AND state NOT IN ('COMPLETED','FAILED','EXPIRED')",
            (state, failure_reason, organization_id, repository_id, request_id),
        )
        return self.get_collection_request(organization_id, repository_id, request_id)

    # -- immutable snapshots -----------------------------------------------

    def submit_metadata_snapshot(self, organization_id, repository_id, environment, *,
                                 snapshot_id, idempotency_key, payload_hash, evidence_hash,
                                 observed_at, collected_at, relations=(), metrics=(),
                                 collector_id=None, collector_version=None,
                                 adapter_type=None, request_id=None, review_id=None,
                                 deployment_id=None, base_sha=None, head_sha=None,
                                 production_deployment_sha=None, base_manifest_hash=None,
                                 head_manifest_hash=None, configuration_version=None,
                                 completeness="COMPLETE", freshness_state="CURRENT",
                                 provenance=None, ttl_seconds=None, expires_at=None):
        """Persist an immutable snapshot with its relation, column and metric
        observations in one transaction.

        Returns ``(snapshot, created)``. A replay of the same idempotency key
        with the same payload returns the original snapshot with
        ``created=False``; a replay with a different payload raises, so a
        conflicting replay can never silently overwrite accepted evidence.
        """
        self._tenant(organization_id, repository_id, environment, allow_disconnected=True)
        existing = self.connection.execute(
            "SELECT * FROM metadata_snapshots WHERE organization_id=%s AND repository_id=%s "
            "AND idempotency_key=%s",
            (organization_id, repository_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                # A conflicting replay must never overwrite accepted evidence.
                # ConflictError so the API boundary reports 409 rather than a
                # generic failure.
                raise SnapshotConflict(
                    "idempotency key already used with a different payload")
            return dict(existing), False

        with self.connection.transaction():
            row = self.connection.execute(
                "INSERT INTO metadata_snapshots (organization_id, repository_id, snapshot_id, "
                "environment, collector_id, collector_version, adapter_type, request_id, "
                "review_id, deployment_id, base_sha, head_sha, production_deployment_sha, "
                "base_manifest_hash, head_manifest_hash, configuration_version, completeness, "
                "freshness_state, provenance, evidence_hash, idempotency_key, payload_hash, "
                "ttl_seconds, observed_at, collected_at, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (organization_id, repository_id, snapshot_id, environment, collector_id,
                 collector_version, adapter_type, request_id, review_id, deployment_id,
                 base_sha, head_sha, production_deployment_sha, base_manifest_hash,
                 head_manifest_hash, configuration_version, completeness, freshness_state,
                 self._Jsonb(provenance or {}), evidence_hash, idempotency_key, payload_hash,
                 ttl_seconds, observed_at, collected_at, expires_at),
            ).fetchone()

            for r_index, relation in enumerate(relations or ()):
                self.connection.execute(
                    "INSERT INTO snapshot_relations (organization_id, repository_id, "
                    "snapshot_id, relation_index, model_unique_id, relation_database, "
                    "relation_schema, relation_name, relation_type, exists_in_production, "
                    "schema_fingerprint, row_count, freshness_timestamp, "
                    "freshness_lag_seconds, lineage_level, lineage_completeness, "
                    "dbt_run_status, dbt_test_status, dbt_execution_ms, collection_status, "
                    "unevaluated_checks, collection_error, observed_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s, %s, %s, %s, %s, %s, %s)",
                    (organization_id, repository_id, snapshot_id, r_index,
                     relation.get("model_unique_id"), relation.get("relation_database"),
                     relation.get("relation_schema"), relation["relation_name"],
                     relation.get("relation_type"),
                     bool(relation.get("exists_in_production", True)),
                     relation.get("schema_fingerprint"), relation.get("row_count"),
                     relation.get("freshness_timestamp"),
                     relation.get("freshness_lag_seconds"), relation.get("lineage_level"),
                     relation.get("lineage_completeness"), relation.get("dbt_run_status"),
                     relation.get("dbt_test_status"), relation.get("dbt_execution_ms"),
                     relation.get("collection_status", "COLLECTED"),
                     self._Jsonb(list(relation.get("unevaluated_checks") or [])),
                     relation.get("collection_error"), relation.get("observed_at")),
                )
                for c_index, column in enumerate(relation.get("columns") or ()):
                    self.connection.execute(
                        "INSERT INTO snapshot_columns (organization_id, repository_id, "
                        "snapshot_id, relation_index, column_index, column_name, "
                        "exists_in_production, data_type, "
                        "is_nullable, ordinal_position, null_count, null_rate, "
                        "duplicate_count, duplicate_rate, distinct_count, cardinality, "
                        "min_value, max_value, collection_status) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                        "%s, %s, %s, %s)",
                        (organization_id, repository_id, snapshot_id, r_index, c_index,
                         column["column_name"],
                         bool(column.get("exists_in_production", True)),
                         column.get("data_type"),
                         column.get("is_nullable"), column.get("ordinal_position"),
                         column.get("null_count"), column.get("null_rate"),
                         column.get("duplicate_count"), column.get("duplicate_rate"),
                         column.get("distinct_count"), column.get("cardinality"),
                         _bounded_text(column.get("min_value")),
                         _bounded_text(column.get("max_value")),
                         column.get("collection_status", "COLLECTED")),
                    )
            for m_index, metric in enumerate(metrics or ()):
                self.connection.execute(
                    "INSERT INTO snapshot_metrics (organization_id, repository_id, "
                    "snapshot_id, metric_index, metric_name, model_unique_id, relation_name, "
                    "metric_value, metric_text, expression_fingerprint, collection_status, "
                    "observed_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (organization_id, repository_id, snapshot_id, m_index,
                     metric["metric_name"], metric.get("model_unique_id"),
                     metric.get("relation_name"), metric.get("metric_value"),
                     _bounded_text(metric.get("metric_text")),
                     metric.get("expression_fingerprint"),
                     metric.get("collection_status", "COLLECTED"),
                     metric.get("observed_at")),
                )
        return dict(row), True

    def get_snapshot(self, organization_id, repository_id, snapshot_id, *, expand=True):
        row = self.connection.execute(
            "SELECT * FROM metadata_snapshots WHERE organization_id=%s AND repository_id=%s "
            "AND snapshot_id=%s",
            (organization_id, repository_id, snapshot_id),
        ).fetchone()
        if row is None:
            return None
        snapshot = dict(row)
        if not expand:
            return snapshot
        relations = [dict(r) for r in self.connection.execute(
            "SELECT * FROM snapshot_relations WHERE organization_id=%s AND repository_id=%s "
            "AND snapshot_id=%s ORDER BY relation_index",
            (organization_id, repository_id, snapshot_id),
        ).fetchall()]
        columns = [dict(c) for c in self.connection.execute(
            "SELECT * FROM snapshot_columns WHERE organization_id=%s AND repository_id=%s "
            "AND snapshot_id=%s ORDER BY relation_index, column_index",
            (organization_id, repository_id, snapshot_id),
        ).fetchall()]
        for relation in relations:
            relation["columns"] = [c for c in columns
                                   if c["relation_index"] == relation["relation_index"]]
        snapshot["relations"] = relations
        snapshot["metrics"] = [dict(m) for m in self.connection.execute(
            "SELECT * FROM snapshot_metrics WHERE organization_id=%s AND repository_id=%s "
            "AND snapshot_id=%s ORDER BY metric_index",
            (organization_id, repository_id, snapshot_id),
        ).fetchall()]
        return snapshot

    def bind_snapshot_to_review(self, organization_id, repository_id, *, review_id,
                                snapshot_id, binding_state, request_id=None,
                                rejection_reason=None, base_sha_match=None,
                                head_sha_match=None, manifest_hash_match=None,
                                freshness_state=None, completeness=None):
        row = self.connection.execute(
            "INSERT INTO snapshot_review_bindings (organization_id, repository_id, review_id, "
            "snapshot_id, request_id, binding_state, rejection_reason, base_sha_match, "
            "head_sha_match, manifest_hash_match, freshness_state, completeness) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (organization_id, repository_id, review_id, snapshot_id) "
            "DO NOTHING RETURNING *",
            (organization_id, repository_id, review_id, snapshot_id, request_id,
             binding_state, rejection_reason, base_sha_match, head_sha_match,
             manifest_hash_match, freshness_state, completeness),
        ).fetchone()
        if row is None:
            row = self.connection.execute(
                "SELECT * FROM snapshot_review_bindings WHERE organization_id=%s "
                "AND repository_id=%s AND review_id=%s AND snapshot_id=%s",
                (organization_id, repository_id, review_id, snapshot_id),
            ).fetchone()
        return dict(row)

    def review_bindings(self, organization_id, repository_id, review_id, *, state=None):
        sql = ("SELECT * FROM snapshot_review_bindings WHERE organization_id=%s "
               "AND repository_id=%s AND review_id=%s")
        args = [organization_id, repository_id, review_id]
        if state is not None:
            sql += " AND binding_state=%s"
            args.append(state)
        sql += " ORDER BY bound_at"
        return [dict(r) for r in self.connection.execute(sql, tuple(args)).fetchall()]

    def latest_accepted_snapshot(self, organization_id, repository_id, review_id):
        row = self.connection.execute(
            "SELECT s.snapshot_id FROM snapshot_review_bindings b "
            "JOIN metadata_snapshots s ON s.organization_id=b.organization_id "
            " AND s.repository_id=b.repository_id AND s.snapshot_id=b.snapshot_id "
            "WHERE b.organization_id=%s AND b.repository_id=%s AND b.review_id=%s "
            "AND b.binding_state='ACCEPTED' ORDER BY s.observed_at DESC LIMIT 1",
            (organization_id, repository_id, review_id),
        ).fetchone()
        if row is None:
            return None
        return self.get_snapshot(organization_id, repository_id, row["snapshot_id"])

    def previous_production_snapshot(self, organization_id, repository_id, environment, *,
                                     snapshot_id, observed_at, received_at=None):
        """The most recent eligible production observation strictly preceding
        this one, for the same tenant, repository and environment.

        "Strictly preceding" is defined against the total order
        ``(observed_at, received_at, snapshot_id)`` rather than against
        ``observed_at`` alone. Two collections can share an observation
        timestamp, and "the previous one" has to be a single, repeatable answer
        - otherwise an idempotent recomputation could pick a different baseline
        on a second run and silently change what an attempt claims.

        The row comparison enforces four things at once, in the database rather
        than in a later filter: the baseline precedes the current snapshot, is
        never the current snapshot, is never from another repository or
        organization, and is never from another environment.

        A snapshot whose collection FAILED or whose freshness is not CURRENT
        is excluded: both remain audit records, but neither is an eligible
        baseline description of production.
        """
        if received_at is None:
            # Only reachable for a snapshot that has not been read back from
            # the database. Falling back to the observation time keeps the
            # ordering total instead of comparing against NULL, which would
            # make the whole row comparison NULL and silently return no
            # baseline at all.
            received_at = observed_at
        row = self.connection.execute(
            "SELECT snapshot_id FROM metadata_snapshots "
            "WHERE organization_id=%s AND repository_id=%s AND environment=%s "
            "AND completeness = ANY(%s) "
            "AND freshness_state = ANY(%s) "
            "AND snapshot_id <> %s "
            "AND (observed_at, received_at, snapshot_id) < (%s, %s, %s) "
            "ORDER BY observed_at DESC, received_at DESC, snapshot_id DESC LIMIT 1",
            (organization_id, repository_id, environment,
             list(ELIGIBLE_COMPLETENESS), list(ELIGIBLE_FRESHNESS), snapshot_id,
             observed_at, received_at, snapshot_id),
        ).fetchone()
        if row is None:
            return None
        return self.get_snapshot(organization_id, repository_id, row["snapshot_id"])

    # -- durable review recomputation --------------------------------------

    def enqueue_review_recomputation(self, organization_id, repository_id, environment, *,
                                     review_id, event_type="review.recompute_requested",
                                     payload=None, dedup_key=None):
        """Enqueue on the same durable outbox the deployment path uses.

        ``dedup_key`` names the unit of work. Exactly-once is per
        (review, event_type, dedup_key), so a redelivered duplicate collapses
        while genuinely new evidence still enqueues. Keying on the review
        alone froze a review's decision at its first snapshot: every later
        snapshot was discarded by ON CONFLICT and never recomputed.

        Callers pass the thing that makes the job distinct - the snapshot id
        for recomputation, the attempt for republication. Omitting it keeps
        the old subject-scoped behaviour, which is what the deployment
        lifecycle events want.
        """
        dedup_key = "" if dedup_key is None else str(dedup_key)
        self.connection.execute(
            "INSERT INTO outbox_events (event_id, organization_id, repository_id, environment, "
            "subject_type, subject_id, deployment_id, event_type, payload, dedup_key) "
            "VALUES (%s, %s, %s, %s, 'review', %s, NULL, %s, %s, %s) "
            "ON CONFLICT (organization_id, repository_id, environment, subject_type, "
            "subject_id, event_type, dedup_key) DO NOTHING",
            (str(uuid.uuid4()), organization_id, repository_id, environment, review_id,
             event_type, self._Jsonb(payload or {"review_id": review_id}), dedup_key),
        )
        row = self.connection.execute(
            "SELECT * FROM outbox_events WHERE organization_id=%s AND repository_id=%s "
            "AND environment=%s AND subject_type='review' AND subject_id=%s "
            "AND event_type=%s AND dedup_key=%s",
            (organization_id, repository_id, environment, review_id, event_type, dedup_key),
        ).fetchone()
        return dict(row) if row else None

    def review_recomputation_jobs(self, organization_id, repository_id, *, review_id=None):
        sql = ("SELECT * FROM review_recomputation_jobs WHERE organization_id=%s "
               "AND repository_id=%s")
        args = [organization_id, repository_id]
        if review_id is not None:
            sql += " AND review_id=%s"
            args.append(review_id)
        sql += " ORDER BY created_at"
        return [dict(r) for r in self.connection.execute(sql, tuple(args)).fetchall()]

    def close(self):
        self.connection.close()
