"""The GitHub Actions workflow Relium hands to a customer.

###################################################################
# ONE FILE, AND IT IS THE ONE RELIUM RUNS AGAINST ITSELF.         #
###################################################################

Setup could tell a customer where the workflow goes and could not give it to
them. The frontend deliberately did not inline a copy: it is ~280 lines of YAML
around an embedded Python submit step whose manifest normalisation is
load-bearing -- it strips exactly the fields dbt stamps afresh on every compile,
and a stale copy would fail re-submission with a 409 nothing on that screen
could explain. A copy that drifts is worse than no copy.

So the backend owns it, and this module is where it lives.

``relium-pr-review.yml`` beside this file is byte-identical to
``.github/workflows/relium-pr-review.yml``, which is the workflow this
repository runs on its own pull requests, and which
``test_hosted_manifest_workflow.py`` already holds to a static security
contract. ``test_ci_workflow_handoff.py`` asserts the two files are the same
bytes, so "the workflow we hand customers is the workflow we run against
ourselves" is a fact rather than an intention.

The packaged copy is the one that ships: the Docker image copies ``agent/`` and
nothing else, so a template under ``.github/`` would not exist in the container
serving the endpoint. ``pyproject.toml`` names it in ``package-data`` for the
same reason on the wheel path.

###################################################################
# THE TEMPLATE CONTAINS NO CREDENTIAL, AND CANNOT.                #
###################################################################

It is served verbatim -- there is no substitution step, no place to inject a
token, and nothing tenant-specific in it. The workflow reads
``secrets.RELIUM_CI_TOKEN`` at run time from the repository secret the customer
created; Relium never puts a value into the file. What IS tenant-specific --
the API URL, the project directory, the manifest path -- travels as repository
VARIABLES, which are not secrets and which the onboarding state already
reports.

That split is deliberate and it is why this can be a static asset rather than a
rendered document. A rendered workflow is a workflow that could one day render
a secret into itself.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

#: Where the workflow belongs in the customer's repository. Named here rather
#: than typed into the UI so the setup screen, the endpoint and any future
#: presence check cannot disagree about it.
WORKFLOW_PATH = ".github/workflows/relium-pr-review.yml"

#: The repository SECRET the workflow authenticates with. Its value is issued
#: by POST /api/onboarding/ci-token, shown once, and never appears here.
CI_TOKEN_SECRET_NAME = "RELIUM_CI_TOKEN"

_SOURCE = Path(__file__).with_name("relium-pr-review.yml")


def workflow_source() -> str:
    """The exact bytes the customer should commit, as text.

    Read from disk on every call rather than cached at import. The file is
    13 kB and this endpoint is hit once per setup; paying that to keep a
    long-lived process from serving a workflow it loaded before the last
    deployment is a trade worth making.
    """
    return _SOURCE.read_text(encoding="utf-8")


def workflow_version(source: str | None = None) -> str:
    """A content-addressed version for the workflow.

    Not a hand-maintained number: a version somebody has to remember to bump is
    a version that is wrong. This is the first 12 hex characters of the SHA-256
    of the file, so it changes exactly when the file does, and two deployments
    reporting the same version are serving the same bytes.
    """
    text = workflow_source() if source is None else source
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def workflow_payload(*, ci_variables=None, ci_token_issued=None) -> dict:
    """The response body for GET /api/onboarding/ci-workflow.

    ``ci_variables`` is the repository-variable mapping the configured
    repository needs, as computed by
    ``agent.api.repository_onboarding.ci_variables_for``. It is passed in
    rather than computed here so there is exactly one place that decides what
    those values are.

    ``ci_token_issued`` is reported so the setup screen can say whether the
    secret the workflow needs exists yet, WITHOUT this response ever carrying
    the secret. It is a boolean and stays one.
    """
    source = workflow_source()
    payload = {
        "path": WORKFLOW_PATH,
        "version": workflow_version(source),
        "content": source,
        "secret_name": CI_TOKEN_SECRET_NAME,
    }
    if ci_variables is not None:
        # Sorted so the response is stable between calls and a diff between two
        # deployments is about the values, never the ordering.
        payload["variables"] = [
            {"name": name, "value": ci_variables[name]}
            for name in sorted(ci_variables)
        ]
    if ci_token_issued is not None:
        payload["ci_token_issued"] = bool(ci_token_issued)
    return payload
