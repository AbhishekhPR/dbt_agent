# Deployment lifecycle evidence

PostgreSQL is the authoritative production lifecycle/evidence store. The
filesystem pilot remains available for deterministic local tests while the
one-way migration is verified. Lifecycle writes and their publication event
are committed together through the outbox.

Allowed success path:

`reviewed → approved → deployment_started → deployment_succeeded →
post_deployment_monitoring → healthy`

Failure paths are explicit: deployment failure enters `deployment_failed`, a
post-deployment signal enters `post_deployment_anomaly`, and recovery proceeds
through `rolled_back` or `incident_open → incident_resolved`. Every transition
is append-only, sequenced, tenant-scoped, idempotent, and attributed to the
deployment's immutable reviewed commit evidence.

The pilot migration exports JSON evidence with SHA-256 checksums and a schema
version, reconciles imported counts/hashes, then switches the writer. Pilot
files are retained as historical evidence; no customer repositories or
warehouses are contacted.
