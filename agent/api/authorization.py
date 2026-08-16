"""Who may do what, in one place.

Two separate questions used to be answered by the same fact — possession of a
tenant service token. That token could read the dashboard, approve a
governance exception, and submit collector metadata, and it was compiled into
the dashboard's JavaScript. This module replaces that with an explicit answer
to each question.

The permission vocabulary is GitHub's, not invented here. ``GET /repos/{owner}/
{repo}`` returns a ``permissions`` object with exactly five booleans:

    {"admin": bool, "maintain": bool, "push": bool, "triage": bool, "pull": bool}

Seeing a repository is not authority over it. ``pull`` — and ``triage``, which
adds issue management but no write access to code — are read. Governance
actions change what happens to a pull request, so they require the authority
to change that pull request: ``push``, ``maintain`` or ``admin``.
"""
from __future__ import annotations

from dataclasses import dataclass

# Ordered most to least authority. Used to name the permission an audit row
# records; never to widen what it grants.
PERMISSION_ORDER = ("admin", "maintain", "push", "triage", "pull")

# The exact GitHub booleans that confer write authority over a pull request.
GOVERNING_PERMISSIONS = frozenset({"admin", "maintain", "push"})


class CapabilityError(Exception):
    """The credential is valid but does not carry the required capability."""


@dataclass(frozen=True)
class Capability:
    """One named thing a caller may be permitted to do."""

    name: str
    #: A human GitHub session may hold this.
    human: bool
    #: A machine service token may hold this, and with which scopes.
    token_scopes: frozenset


def _cap(name, *, human=False, token_scopes=()):
    return Capability(name, human, frozenset(token_scopes))


#: Reading the dashboard. Not granted to a collector: a collector needs its own
#: work, not a view of the tenant's review history.
DASHBOARD_READ = _cap("dashboard_read", human=True, token_scopes={"operator_read"})

#: Changing what happens to a pull request. Human only, and only a human with
#: write authority on the repository. No service token scope grants this — a
#: leaked machine credential must not be able to approve an exception.
GOVERNANCE_WRITE = _cap("governance_write", human=True)

#: The collector's own work: claiming a request, reporting its outcome,
#: submitting a snapshot. Machine only. A person's browser session is not an
#: ingestion credential, so a human session is refused here even though it is
#: strictly more trusted for everything else.
COLLECTOR_INGEST = _cap("collector_ingest", token_scopes={"collector"})

#: Deployment and monitoring ingestion from a customer's own pipeline.
PIPELINE_INGEST = _cap("pipeline_ingest", token_scopes={"collector"})

# A CI credential may hand off the generated dbt manifest for one exact
# commit. It cannot read the dashboard, submit warehouse observations, or
# perform governance. Keeping it separate limits a workflow-token leak to the
# one artifact channel the workflow actually needs.
CI_MANIFEST_INGEST = _cap("ci_manifest_ingest", token_scopes={"ci"})

#: One collection request, by id.
#:
#: The only endpoint both kinds of caller genuinely need: the collector reads
#: the request it is about to satisfy, and the dashboard shows the operator
#: what was asked for. It is listed separately rather than by relaxing the
#: rule for reads in general, so the overlap stays visible and stays at one.
COLLECTION_REQUEST_READ = _cap(
    "collection_request_read", human=True, token_scopes={"collector", "operator_read"})


def highest_permission(permissions) -> str | None:
    """Name the strongest GitHub permission present, or None if the user has none."""
    if not isinstance(permissions, dict):
        return None
    for name in PERMISSION_ORDER:
        if permissions.get(name) is True:
            return name
    return None


def may_govern(permissions) -> bool:
    """True when GitHub reports write authority over the repository.

    ``triage`` is deliberately excluded. It allows managing issues and pull
    requests without the ability to push, and Relium's governance actions
    change whether code merges.
    """
    if not isinstance(permissions, dict):
        return False
    return any(permissions.get(name) is True for name in GOVERNING_PERMISSIONS)


def may_read(permissions) -> bool:
    """True when GitHub reports any access to the repository at all."""
    if not isinstance(permissions, dict):
        return False
    return any(permissions.get(name) is True for name in PERMISSION_ORDER)


def authorize(principal, capability: Capability) -> None:
    """Raise unless ``principal`` carries ``capability``.

    Every authenticated route passes through here. A route that needs a new
    capability declares one above rather than testing a principal's shape
    inline, so the whole policy stays readable in one file.
    """
    if principal is None:
        raise CapabilityError("no authenticated principal")

    if principal.is_human:
        if not capability.human:
            raise CapabilityError(
                f"{capability.name} is not available to a dashboard session")
        if capability is GOVERNANCE_WRITE and not principal.may_govern:
            raise CapabilityError(
                "this GitHub account does not have write access to the repository")
        return

    if capability.token_scopes and principal.scope in capability.token_scopes:
        return
    raise CapabilityError(
        f"{capability.name} is not available to a {principal.scope} token")
