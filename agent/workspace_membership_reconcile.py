"""Explicit dry-run-first reconciliation of legacy workspace membership."""
from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import datetime, timezone

from agent.api.clerk_management import ClerkManagementClient, ClerkManagementSettings
from agent.api.workspace_membership import project_memberships


def reconcile_tenant_memberships(store, source, *, apply=False, clock=None,
                                 generation_factory=None):
    """Inspect every tenant against Clerk; persist only when explicitly applied."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    generation_factory = generation_factory or (
        lambda: f"wms_{uuid.uuid4().hex}")
    report = []
    for tenant in store.tenants_for_membership_reconciliation():
        attempted_at = clock()
        generation = generation_factory()
        try:
            if apply:
                store.begin_tenant_membership_sync(
                    tenant_id=tenant["tenant_id"],
                    clerk_organization_id=tenant["clerk_organization_id"],
                    sync_generation=generation,
                )
            snapshot = source.organization_snapshot(
                tenant["clerk_organization_id"])
            projection = project_memberships(snapshot)
            if apply:
                store.replace_tenant_membership_projection(
                    tenant_id=tenant["tenant_id"], projection=projection,
                    sync_generation=generation, synchronized_at=attempted_at,
                )
            report.append({
                "tenant_id": tenant["tenant_id"],
                "status": "applied" if apply else "dry_run",
                "ownership_status": projection.ownership_status,
                "active_member_count": len(projection.memberships),
                "active_owner_count": projection.active_owner_count,
            })
        except Exception:
            if apply:
                try:
                    store.record_tenant_membership_sync_failure(
                        tenant_id=tenant["tenant_id"],
                        sync_generation=generation,
                        attempted_at=attempted_at,
                        failure_category="reconciliation_failed",
                    )
                except Exception:
                    pass
            report.append({
                "tenant_id": tenant["tenant_id"],
                "status": "failed",
                "ownership_status": "ambiguous",
                "active_member_count": None,
                "active_owner_count": None,
            })
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Reconcile Relium workspace membership from Clerk")
    parser.add_argument(
        "--apply", action="store_true",
        help="persist complete Clerk snapshots; without this flag the command is read-only",
    )
    args = parser.parse_args(argv)
    dsn = os.environ.get("RELIUM_DATABASE_URL")
    settings = ClerkManagementSettings.from_environ(os.environ)
    if not dsn:
        parser.error("RELIUM_DATABASE_URL is required")
    if settings is None:
        parser.error("RELIUM_CLERK_SECRET_KEY is required")

    from agent.postgres_lifecycle_store import PostgresLifecycleStore

    store = PostgresLifecycleStore(dsn)
    try:
        report = reconcile_tenant_memberships(
            store, ClerkManagementClient(settings), apply=args.apply)
    finally:
        store.close()
    print(json.dumps({"apply": args.apply, "tenants": report}, sort_keys=True))
    return 1 if any(row["status"] == "failed" for row in report) else 0


if __name__ == "__main__":
    raise SystemExit(main())
