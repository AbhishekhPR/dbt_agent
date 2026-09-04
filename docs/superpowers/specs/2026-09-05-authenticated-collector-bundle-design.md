# Authenticated Collector Bundle Design

## Goal

Let a new customer download, verify, install, configure, and test the Relium
collector using only the Integrations screen and a clean Python installation.
No repository checkout, GitHub Actions access, guessed filename, or Relium
development environment is required.

## Current-state finding

The collector is not currently published to a customer-accessible endpoint.
`.github/workflows/collector-package.yml` is manual and uploads only a GitHub
Actions artifact. The repository has no GitHub Releases, and the collector
workflow has not produced a collector artifact. Consequently, the dashboard's
references to `relium-0.1.0-py3-none-any.whl` and `SHA256SUMS` name files that a
customer cannot obtain.

A wheel built from the current package configuration is named
`relium-0.1.0-py3-none-any.whl`. Its installed metadata declares the console
entry point `relium = agent.cli:cli`. The package contains neither
`relium/__main__.py` nor `agent/__main__.py`, so module-style invocations are
not supported. The customer command is `relium collect --test` through the
virtual environment's installed console script.

## Distribution architecture

The production Docker build creates the wheel once in a dedicated builder
stage. That same stage writes `SHA256SUMS` from the wheel bytes and packages
both files into `relium-collector-0.1.0.zip`. The runtime image copies this
finished ZIP without rebuilding or modifying it. The generic artifact contains
no tenant, repository, environment, token, DSN, deployment URL, or other
customer-specific data.

The API serves those exact ZIP bytes from a read-only file path. A new
`GET /api/collector-package` route uses the normal dashboard-read
authentication and the `warehouse_evidence` plan entitlement. It returns an
attachment named `relium-collector-0.1.0.zip`; errors remain ordinary JSON API
errors. Collector service tokens cannot access the route.

The existing manual packaging workflow remains a CI verification mechanism,
not a distribution endpoint. This launch adds no GitHub Release or object
storage infrastructure.

## Dashboard flow

The Warehouse evidence panel starts with a real **Download collector** action.
The frontend fetches `/api/collector-package` with the authenticated session,
uses the server-provided attachment filename, and saves the returned bytes.
Download failures are explicit and do not claim that anything was saved.

The panel provides two instruction sets:

- Windows PowerShell extracts the ZIP, reads the expected digest from
  `SHA256SUMS`, calculates the wheel digest with `Get-FileHash`, refuses a
  mismatch, creates `.venv`, installs the named wheel, sets the required
  environment variables, and runs `.venv\\Scripts\\relium.exe collect --test`.
- macOS/Linux extracts the ZIP, verifies with `sha256sum` when available or
  macOS `shasum -a 256` otherwise, creates `.venv`, installs the named wheel,
  exports the required environment variables, and runs
  `.venv/bin/relium collect --test`.

The commands use only files delivered by the download and tools available on a
clean supported host after installing Python and an unzip utility. They never
reference a local source tree or GitHub Actions.

## Interfaces and failure handling

The backend download handler supports binary payloads and an explicit media
type without changing existing Markdown or JSON downloads. It validates that
the configured artifact is a regular file and fails closed if the image was
built incorrectly. The response uses `application/zip`,
`Content-Disposition: attachment`, and `Cache-Control: private, no-store` so a
shared cache cannot replay authenticated content.

The frontend API helper uses `credentials: include`, does not cache the
response, maps authentication/authorization errors through the existing API
error model, and trusts the server's filename only through the bounded
`Content-Disposition` parser already used for other downloads.

## Verification

Backend tests first prove the missing route and binary response behavior, then
cover authentication, entitlement enforcement, exact bytes, headers, filename,
and a Docker build contract that builds and copies the bundle once. Packaging
tests inspect the produced ZIP, verify `SHA256SUMS`, install the enclosed wheel
into a new virtual environment, and invoke the installed `relium` console
script.

Frontend tests first prove the missing action and OS-specific instructions,
then cover the authenticated blob request, successful save, server filename,
failure state, exact wheel name, checksum commands, environment configuration,
and the supported `relium collect --test` invocation. Final verification runs
focused backend and frontend tests, the complete relevant suites, a production
frontend build, a Docker image build, and clean temporary-environment installs.

No merge or deployment is part of this work.
