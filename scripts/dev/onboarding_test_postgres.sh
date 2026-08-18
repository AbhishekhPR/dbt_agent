#!/usr/bin/env bash
# Provision the ISOLATED LOCAL PostgreSQL used by the onboarding backend tests.
#
# This is a throwaway container on a non-default port. It is not Railway, not
# production, and shares nothing with either. Destroying it loses nothing.
#
# It mirrors .github/workflows/test.yml deliberately: the application role is
# least-privileged (NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOREPLICATION,
# NOBYPASSRLS) and owns only its own database. Running the suite as a superuser
# would pass tests that production would fail — and
# test_postgres_lifecycle_store.py asserts exactly that, so a superuser DSN is
# caught rather than silently tolerated.
#
#   bash scripts/dev/onboarding_test_postgres.sh
#   export RELIUM_TEST_POSTGRES_DSN="postgresql://relium_validation:relium_validation_local_password@127.0.0.1:55461/relium_validation"
#
# The password is a local-only literal for a container that accepts connections
# from 127.0.0.1 alone. It is not a credential for anything that exists outside
# this machine.
set -euo pipefail

CONTAINER=relium-onboarding-pg
PORT=55461                      # deliberately not 5432, and not the smoke stack's 55443
ADMIN_PASSWORD=supersecret
APP_ROLE=relium_validation
APP_PASSWORD=relium_validation_local_password
APP_DB=relium_validation

docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true

docker run -d --name "${CONTAINER}" \
  -e POSTGRES_PASSWORD="${ADMIN_PASSWORD}" \
  -p "127.0.0.1:${PORT}:5432" \
  postgres:16 >/dev/null

for _ in $(seq 1 30); do
  if docker exec "${CONTAINER}" pg_isready -U postgres >/dev/null 2>&1; then break; fi
  sleep 2
done

# Separate -c invocations: CREATE DATABASE cannot run inside a transaction
# block, and psql wraps a multi-statement -c in one.
docker exec "${CONTAINER}" psql -U postgres -d postgres -v ON_ERROR_STOP=1 -c \
  "CREATE ROLE ${APP_ROLE} WITH LOGIN PASSWORD '${APP_PASSWORD}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 10;"
docker exec "${CONTAINER}" psql -U postgres -d postgres -v ON_ERROR_STOP=1 -c \
  "CREATE DATABASE ${APP_DB} OWNER ${APP_ROLE};"
docker exec "${CONTAINER}" psql -U postgres -d postgres -v ON_ERROR_STOP=1 -c \
  "REVOKE ALL ON DATABASE ${APP_DB} FROM PUBLIC;"
docker exec "${CONTAINER}" psql -U postgres -d postgres -v ON_ERROR_STOP=1 -c \
  "GRANT CONNECT, TEMP ON DATABASE ${APP_DB} TO ${APP_ROLE};"

# Same assertion CI makes, for the same reason.
verdict="$(docker exec "${CONTAINER}" psql -U postgres -d "${APP_DB}" -tAc \
  "SELECT CASE WHEN rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls THEN 'LEAST_PRIVILEGED' ELSE 'OVER_PRIVILEGED' END FROM pg_roles WHERE rolname='${APP_ROLE}'")"
if [ "${verdict}" != "LEAST_PRIVILEGED" ]; then
  echo "ERROR: ${APP_ROLE} is not a least-privileged login role (${verdict})" >&2
  exit 1
fi

echo "${verdict}"
echo "RELIUM_TEST_POSTGRES_DSN=postgresql://${APP_ROLE}:${APP_PASSWORD}@127.0.0.1:${PORT}/${APP_DB}"
