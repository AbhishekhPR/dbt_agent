"""Review lifecycle service used by the served GitHub review path.

This is the dependency `PullRequestReviewRunner` receives so the production
webhook path reaches the PostgreSQL review lifecycle. It exists because
Release 1 built the lifecycle machinery and tested it directly, but never
connected it to the runner - so a genuine webhook still wrote only to the
filesystem store.

The runner never opens a database connection itself and never reaches for
global state; it holds this object or it holds nothing.
"""
from __future__ import annotations

import logging

from agent.metadata_evidence.review_lifecycle import begin_review
from agent.metadata_evidence.manifest_handoff import begin_manifest_wait

logger = logging.getLogger(__name__)

DEFAULT_ENVIRONMENT = "production"


class LifecycleUnavailable(RuntimeError):
    """PostgreSQL metadata review is enabled but its dependency is missing.

    Raised at startup rather than tolerated, so a production deployment can
    never silently degrade to filesystem-only review while advertising the
    metadata lifecycle.
    """


class ReviewLifecycleService:
    """Thin, explicit seam between the GitHub runner and the review lifecycle."""

    mode = "postgresql"

    def __init__(self, store_pool, *, environment=DEFAULT_ENVIRONMENT):
        if store_pool is None:
            raise LifecycleUnavailable(
                "metadata review lifecycle requires a PostgreSQL store pool")
        self._pool = store_pool
        self.environment = environment

    @property
    def enabled(self) -> bool:
        return True

    def begin(self, *, organization_id, repository_id, pull_number, base_sha,
              head_sha, base_manifest, head_manifest, changed_models,
              enforcement_mode, delivery_id=None, code_health=100,
              code_findings=(), critical_models=(), environment=None,
              semantic_evidence=None):
        """Persist the review and decide whether production evidence is needed."""
        with self._pool.acquire() as store:
            return begin_review(
                store,
                organization_id=organization_id,
                repository_id=repository_id,
                environment=environment or self.environment,
                pull_number=pull_number,
                base_sha=base_sha,
                head_sha=head_sha,
                semantic_evidence=semantic_evidence,
                base_manifest=base_manifest,
                head_manifest=head_manifest,
                changed_models=changed_models,
                enforcement_mode=enforcement_mode,
                delivery_id=delivery_id,
                code_health=code_health,
                code_findings=code_findings,
                critical_models=critical_models,
            )

    def record_publication(self, *, organization_id, repository_id, review_id,
                           comment_id=None, check_run_id=None):
        """Remember the sticky comment and check run for this review.

        Recomputation reconciles these identities instead of publishing again.
        """
        with self._pool.acquire() as store:
            return store.record_review_publication(
                organization_id, repository_id, review_id,
                comment_id=comment_id, check_run_id=check_run_id)

    def wait_for_manifest(self, *, organization_id, repository_id, pull_number,
                          base_sha, head_sha, base_manifest, head_manifest=None,
                          changed_files=(),
                          enforcement_mode, delivery_id=None, environment=None):
        with self._pool.acquire() as store:
            return begin_manifest_wait(
                store,
                organization_id=organization_id, repository_id=repository_id,
                environment=environment or self.environment,
                pull_number=pull_number, base_sha=base_sha, head_sha=head_sha,
                base_manifest=base_manifest, head_manifest=head_manifest,
                changed_files=changed_files,
                enforcement_mode=enforcement_mode, delivery_id=delivery_id,
            )

    def get_manifest_evidence(self, *, organization_id, repository_id,
                              commit_sha):
        with self._pool.acquire() as store:
            return store.get_manifest_evidence(
                organization_id, repository_id, commit_sha)

    def get_review(self, *, organization_id, repository_id, review_id):
        with self._pool.acquire() as store:
            return store.get_review(organization_id, repository_id, review_id)


class DisabledReviewLifecycle:
    """Deterministic local compatibility mode.

    Used only where filesystem-only review behaviour is explicitly intended -
    unit tests and local runs without a database. It is inert by design and
    reports itself so the active mode is never ambiguous.
    """

    mode = "filesystem-compatibility"
    enabled = False

    def begin(self, **_kwargs):
        return None

    def record_publication(self, **_kwargs):
        return None

    def wait_for_manifest(self, **_kwargs):
        return None

    def get_manifest_evidence(self, **_kwargs):
        return None

    def get_review(self, **_kwargs):
        return None


def build_review_lifecycle(store_pool, *, metadata_review_enabled,
                           environment=DEFAULT_ENVIRONMENT):
    """Resolve the lifecycle dependency for the served composition root.

    A configuration that declares metadata review enabled but supplies no
    PostgreSQL dependency fails here rather than starting in a degraded mode
    that looks healthy.
    """
    if metadata_review_enabled:
        if store_pool is None:
            raise LifecycleUnavailable(
                "metadata review is enabled but no database is configured; "
                "set the database URL or disable metadata review explicitly")
        return ReviewLifecycleService(store_pool, environment=environment)
    if store_pool is not None:
        logger.info("metadata review lifecycle available but not enabled")
    return DisabledReviewLifecycle()
