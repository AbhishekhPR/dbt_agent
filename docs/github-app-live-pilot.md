# Relium GitHub App live pilot

This guide runs one local Relium process for one dedicated test repository. It
does not create permanent infrastructure. Keep the server and tunnel terminals
running throughout the pilot.

## A. Create the GitHub App

In GitHub, open **Settings → Developer settings → GitHub Apps → New GitHub App**
and configure:

- Suggested name: **Relium Pilot**
- Homepage URL: `https://www.relium.dev`
- Webhook URL: `https://<temporary-tunnel-host>/github/webhook`
- Webhook content type: `application/json`
- Webhook secret: a cryptographically strong random value stored in a password
  manager or secret store

Set repository permissions exactly as follows:

- Contents: Read
- Issues: Read and write
- Pull requests: Read and write
- Checks: Read and write
- Metadata: Read

Subscribe to **Pull request** and no other event for this pilot. GitHub represents
pull-request discussion comments through the Issues API, so Issues write access is
required even though Relium comments on a pull request. Pull requests write access
allows the App to publish on pull-request subjects as well as receive and inspect
the pull-request event; Checks write access allows it to publish the Relium check
run.

Generate the webhook secret with a local password manager or a command such as
`openssl rand -hex 32`. Place it only in the ignored local environment file below;
do not paste it into shell history.

## B. Generate and protect the private key

From the GitHub App settings page, select **Generate a private key**. Move the
downloaded PEM file outside this repository, restrict it to your user, and point
Relium at it:

```bash
mkdir -p ~/.config/relium/github-app
mv ~/Downloads/your-downloaded-key.pem ~/.config/relium/github-app/relium-pilot.private-key.pem
chmod 600 ~/.config/relium/github-app/relium-pilot.private-key.pem
```

Set `RELIUM_GITHUB_PRIVATE_KEY_PATH` to that absolute path. Never copy the key into
the repository or commit it. The repository ignores PEM patterns as a final safety
net, not as permission to store keys here.

## C. Install the App on one repository

Open the App's **Install App** page, choose the pilot account, select **Only select
repositories**, and grant access to one dedicated test repository. Do not select
**All repositories** during the pilot.

## D. Configure and start Relium

Create the ignored local environment file from the non-secret template:

```bash
cp .env.github-app.example .env.github-app
chmod 600 .env.github-app
```

Fill in the App ID, webhook secret, absolute private-key path, and desired local
storage path. Do not print the completed file. Then run:

```bash
source .venv/bin/activate
set -a
source .env.github-app
set +a
scripts/github_app_pilot_preflight.sh
python -m agent.github_app.server
```

The equivalent convenience command after loading the environment is:

```bash
scripts/github_app_pilot_start.sh
```

Run exactly one server process. A successful start binds to `127.0.0.1:8000` by
default and starts the bounded queue and workers during application lifespan.
SIGINT (`Ctrl+C`) and SIGTERM trigger Uvicorn's graceful shutdown path.

## E. Verify health

In another terminal, run:

```bash
curl --fail http://127.0.0.1:8000/healthz
```

Expected response:

```json
{"status":"ok"}
```

The checked helper is also available:

```bash
scripts/github_app_pilot_health.sh
```

## F. Expose the webhook through HTTPS

Use any trusted HTTPS tunnel that forwards its public endpoint to
`http://127.0.0.1:8000` without changing request bytes or GitHub headers. Keep the
tunnel process local and temporary; Relium does not depend on a particular tunnel
provider.

Copy the tunnel's HTTPS origin into the GitHub App settings and append the required
path. The final public webhook URL must have this form and must end with
`/github/webhook`:

```text
https://<temporary-tunnel-host>/github/webhook
```

Do not use `/healthz` as the GitHub webhook URL.

## G. Prepare the test repository

The dedicated repository must contain:

- `relium.yml` at the repository root;
- `target/manifest.json` committed for this controlled pilot;
- at least one dbt model represented in that manifest; and
- a pull request that changes the represented model.

Use this minimal `relium.yml`:

```yaml
manifest_path: target/manifest.json
enforcement_mode: shadow
enabled: true
```

`enforcement_mode` is the sole GitHub check-enforcement setting. `shadow` is the
safe default for repositories that omit it. The legacy `mode` is deprecated and
accepted only for configuration compatibility; it does not change the GitHub
check conclusion.

The fixture in `demo/github_app_pilot/` contains previous/current manifests and
safe/risky model examples. Copy only appropriate non-secret fixture material into
the dedicated repository. Do not copy local Relium storage.

## H. Run the pilot scenarios

Processing is asynchronous. A `202` means the signed delivery was accepted or
truthfully ignored; inspect the PR and safe server logs for the eventual outcome.
Delivery state is stored under `RELIUM_STORAGE_ROOT/<repository-id>/deliveries/`.

| Scenario | Webhook response | Expected PR comment | Check-run conclusion | Storage behavior |
| --- | --- | --- | --- | --- |
| 1. Valid PR with changed dbt model | `202 accepted` | One App-owned Relium review with the computed decision | `success` for ALLOW; `neutral` for WARN or BLOCK in shadow mode | Delivery becomes `complete` |
| 2. Missing manifest | `202 accepted` | Neutral message: `Relium could not find target/manifest.json. Run dbt compile before the Relium review.` | `neutral` | Delivery becomes `complete` |
| 3. No changed dbt model | `202 accepted` | Neutral message explaining that no dbt models changed | `neutral` | Delivery becomes `complete` |
| 4. Re-delivered webhook | `202 accepted` | No duplicate comment or publication | No additional check run | Existing completed delivery claim is preserved |
| 5. BLOCK result in shadow mode | `202 accepted` | Review shows the BLOCK decision and actionable findings | `neutral`, so it does not block merging | Delivery becomes `complete` |
| 6. BLOCK result in enforce mode | `202 accepted` | The same review comment as shadow mode | `failure` | Delivery becomes `complete` |

`enforcement_mode` controls both the GitHub check conclusion and the workflow exit
code. In `shadow`, ALLOW succeeds while WARN and BLOCK remain advisory and
non-failing. In `enforce`, BLOCK fails while ALLOW succeeds and WARN remains
advisory.

For scenario 6, change only `enforcement_mode: shadow` to
`enforcement_mode: enforce` in the test repository's `relium.yml`, then open or
synchronize a new test pull request. Do not change Relium's decision thresholds to
manufacture an outcome; use the risky fixture or a naturally blocking test change.

## I. Troubleshooting

### Invalid signature

GitHub receives `401`. Confirm that the App and `RELIUM_GITHUB_WEBHOOK_SECRET` use
the same secret and that the tunnel preserves the exact body. Invalid deliveries
are not queued and create no storage claim.

### Missing installation token

The initial signed request can still return `202`, but background processing fails
with a safe authentication category. Confirm the App is installed on the test
repository and that the delivery includes an installation ID. No token is logged.

### Incorrect App ID

Confirm `RELIUM_GITHUB_APP_ID` is the numeric App ID from the App settings, not an
installation ID or client ID. Authentication failures are not retried.

### Inaccessible private key

Run `scripts/github_app_pilot_preflight.sh`. Confirm the configured file exists,
is readable by the current user, and has mode `600`. The scripts do not print the
path or key contents on failure.

### Queue full

GitHub receives `503`. Wait for accepted work to finish and redeliver from GitHub.
Do not increase worker or queue limits until the pilot's resource use is understood.
No delivery claim exists for a request that could not be queued.

### Missing manifest

Confirm `relium.yml` points to `target/manifest.json` and that the file exists at
the PR head. The result is a neutral actionable comment and neutral check, not a
claimed semantic review.

### No comment permission

Confirm **Issues: Read and write** is granted and reinstall or approve updated App
permissions if GitHub requests it. A `403` is not retried. If comment publication
fails, the delivery claim is released for a later redelivery.

### No check-run permission

Confirm **Checks: Read and write**. A comment may already have been upserted before
check publication fails; after permissions are corrected, redelivery updates the
owned comment and retries check publication.

### Duplicate delivery

A repeated GitHub delivery ID is accepted at the HTTP boundary and then reported
as duplicate by the repository-scoped delivery claim. It should not create another
comment or check run. Use a new pull-request synchronization event when a new
review is intended.

### GitHub webhook delivery logs

In the GitHub App settings, open **Advanced → Recent deliveries**. Inspect the
event and delivery ID plus the response status (`202`, `401`, `400`, `413`, or
`503`). Use **Redeliver** only after correcting the underlying issue. Never copy
authorization tokens, signatures, or full private payloads into tickets or chat.
