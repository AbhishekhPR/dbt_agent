# Authenticated Collector Bundle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship one immutable generic collector bundle in the production image and let entitled dashboard users download, verify, install, configure, and test it on clean Windows, macOS, or Linux hosts.

**Architecture:** A Docker builder stage creates the wheel, derives `SHA256SUMS` from those exact bytes, and creates one versioned ZIP copied unchanged into the runtime image. The authenticated API serves that file through the existing dashboard-read and plan-entitlement wrapper; the frontend downloads the server-owned bytes and renders literal OS-correct commands using the package's real console entry point.

**Tech Stack:** Python 3.10, setuptools/build, Starlette, Docker multi-stage builds, React, Vitest, PowerShell, POSIX shell.

---

## File structure

- `Dockerfile`: build the generic wheel/checksum ZIP once and copy it into the runtime image.
- `agent/api/collector_package.py`: own stable artifact names/path and validate the packaged file.
- `agent/api/routes.py`: authorize and serve the binary attachment.
- `agent/api/contract.py`, `docs/api-contract.json`, `docs/public-api.md`: publish the actually served route contract.
- `test_collector_package.py`, `test_collector_install.py`: cover binary serving, authorization contract, build contract, ZIP integrity, installed entry point, and clean-host flow.
- `src/lib/api.js`, `src/lib/api.test.js`: fetch the authenticated artifact as a blob.
- `src/pages/Integrations.jsx`, `src/pages/Integrations.collector.test.jsx`: provide the download action and exact OS-specific instructions.
- `src/styles/app.css`: style the download state and OS instruction blocks.

### Task 1: Production artifact build contract

- [ ] Add failing tests to `test_collector_install.py` asserting that `Dockerfile` has a collector builder stage, builds `relium-0.1.0-py3-none-any.whl` once, creates `SHA256SUMS` beside that wheel, creates `relium-collector-0.1.0.zip`, copies only the completed ZIP into the runtime stage, and never embeds credential/environment names in bundle creation.
- [ ] Run `python -m pytest test_collector_install.py -q`; expect the new tests to fail because the Dockerfile currently copies only `agent/`.
- [ ] Modify `Dockerfile` to copy `pyproject.toml`, `README.md`, and `agent/` into a builder stage; install a pinned build frontend; run `python -m build --wheel`; require exactly `relium-0.1.0-py3-none-any.whl`; generate and verify `SHA256SUMS`; create a ZIP containing only the wheel and checksum; then copy that ZIP to `/app/artifacts/relium-collector-0.1.0.zip` in the runtime stage.
- [ ] Re-run `python -m pytest test_collector_install.py -q`; expect PASS.

### Task 2: Authenticated entitled download endpoint

- [ ] Create failing tests in `test_collector_package.py` that build a two-file ZIP fixture and assert `GET /api/collector-package` is present, returns its exact bytes as `application/zip`, names `relium-collector-0.1.0.zip`, sets `Cache-Control: private, no-store`, rejects an unauthenticated caller, rejects a dashboard user without `warehouse_evidence`, and cannot be authorized by a collector token.
- [ ] Run `python -m pytest test_collector_package.py test_api_contract.py -q`; expect failures for the absent module and route.
- [ ] Add `agent/api/collector_package.py` with constants `COLLECTOR_VERSION = "0.1.0"`, `COLLECTOR_WHEEL_FILENAME = "relium-0.1.0-py3-none-any.whl"`, `COLLECTOR_BUNDLE_FILENAME = "relium-collector-0.1.0.zip"`, a default `/app/artifacts/...` path, and a resolver that accepts a deployment/test path but fails closed unless it is a regular file.
- [ ] Extend the existing authenticated download wrapper in `agent/api/routes.py` to serve a validated `Path` with `FileResponse`, explicit `application/zip`, attachment disposition, request id, and private no-store caching. Register `GET /api/collector-package` with the human-only `COLLECTOR_PACKAGE_DOWNLOAD` capability and `plan_capability=WAREHOUSE_EVIDENCE`.
- [ ] Add the route to `MANDATORY_ROUTES`, regenerate `docs/api-contract.json`, and document the endpoint in `docs/public-api.md`.
- [ ] Re-run the focused backend tests; expect PASS.

### Task 3: Authenticated frontend download client

- [ ] Add a failing `src/lib/api.test.js` case for `downloadCollectorPackage()` asserting URL `/api/collector-package`, `credentials: "include"`, `cache: "no-store"`, `Accept: "application/zip"`, exact blob bytes, and the filename from `Content-Disposition`.
- [ ] Run `npm run test:unit -- src/lib/api.test.js` with `VITE_RELIUM_API_URL=http://127.0.0.1:8099`; expect failure because the helper is absent.
- [ ] Implement `downloadCollectorPackage()` in `src/lib/api.js` using the existing download error translation, returning `{ blob, filename }` with `relium-collector-0.1.0.zip` only as a bounded fallback.
- [ ] Re-run the focused API test; expect PASS.

### Task 4: Download action and OS-specific instructions

- [ ] Add failing tests to `src/pages/Integrations.collector.test.jsx` that assert a real **Download collector** button, successful blob save, explicit failure state, exact stable ZIP/wheel names, a Windows PowerShell flow using `Expand-Archive`, `Get-FileHash`, `.venv\\Scripts\\python.exe`, and `.venv\\Scripts\\relium.exe collect --test`, plus macOS/Linux flows using `unzip`, `sha256sum` or `shasum -a 256`, `.venv/bin/python`, and `.venv/bin/relium collect --test`. Assert neither `python -m relium` nor `python -m agent` appears.
- [ ] Run `npm run test:unit -- src/pages/Integrations.collector.test.jsx`; expect the new tests to fail against the static nonexistent-file instructions.
- [ ] Wire `WarehouseEvidencePanel` to `downloadCollectorPackage()`, save the server blob with an object URL, revoke the URL, and report success/failure honestly. Render separate Windows PowerShell, macOS, and Linux instruction blocks that configure `RELIUM_API_URL`, `RELIUM_API_TOKEN`, `RELIUM_WAREHOUSE_DSN`, `RELIUM_ENVIRONMENT`, and `RELIUM_COLLECTOR_ID` only on the customer's host.
- [ ] Update `src/styles/app.css` for compact OS command sections and download status.
- [ ] Re-run focused component tests; expect PASS.

### Task 5: Clean-machine-style package acceptance

- [ ] Add a failing acceptance test to `test_collector_package.py` that builds the project wheel in a temporary source copy, writes and checks the checksum, creates the stable ZIP, extracts it into a second empty directory, independently verifies the digest, creates a fresh virtual environment, installs the enclosed wheel, and invokes the installed `relium collect --test` console script with only customer-side environment variables and a local fake API/PostgreSQL boundary. Assert package metadata maps `relium` to `agent.cli:cli` and no module invocation is used.
- [ ] Run the acceptance test and confirm its initial failure identifies the missing packaging helper/build output contract.
- [ ] Add only the minimal test support needed to exercise the existing collector test path; do not add production test-only APIs or put DSNs/tokens into the artifact.
- [ ] Run the acceptance test on Windows. Run the bundle's POSIX checksum/install/entry-point sequence in a Linux Docker container to cover Linux; validate the macOS branch through `shasum -a 256` format compatibility and command-contract tests when a macOS runner is unavailable.

### Task 6: Full verification and PR

- [ ] Run backend focused tests, the backend full unit suite, wheel/ZIP inspection, checksum verification, package metadata inspection, and Docker image build.
- [ ] Run frontend focused tests, the full suite with the required API URL environment, secret bundle scan, and production build.
- [ ] Confirm both worktrees contain only intended changes and no artifact contains any token, DSN, workspace, repository, or environment value.
- [ ] Commit backend and frontend changes separately, push both named branches, and create or update pull requests without merging or deploying.
- [ ] Report artifact source, exact filenames and ZIP contents, real CLI entry point, clean-machine evidence by OS, PR links, deployment implications, and remaining blockers.
