-- Backfill authoritative operational-root ownership after migration 0021 has
-- committed and released its DDL locks. Reads do not block normal operational
-- writes. Bound the scan so startup fails and retries instead of leaving an
-- unnoticed long-running migration.
SET LOCAL statement_timeout = '5min';

-- The only automatic proof uses the opaque CI token identifier written by the
-- authenticated onboarding flow. Every repository under the root must have
-- exactly one internally consistent chain, and every chain must resolve to the
-- same tenant. Partial, duplicate, broken and cross-tenant candidates insert
-- nothing. The source repository/token pair is selected from one candidate
-- row; independent minima could fabricate a pair that never existed.
WITH repository_candidates AS (
    SELECT
        r.organization_id,
        r.repository_id,
        count(candidate.tenant_id) AS candidate_count,
        min(candidate.tenant_id) AS tenant_id,
        min(candidate.github_repository_id) AS github_repository_id,
        min(candidate.ci_token_id) AS ci_token_id
    FROM repositories r
    LEFT JOIN LATERAL (
        SELECT
            tr.tenant_id,
            tr.github_repository_id,
            tr.ci_token_id
        FROM tenant_repositories tr
        JOIN api_service_tokens token
          ON token.token_id = tr.ci_token_id
         AND token.scope = 'ci'
         AND token.organization_id = r.organization_id
         AND token.repository_id = r.repository_id
        JOIN tenant_github_installations installation
          ON installation.github_installation_id = tr.github_installation_id
         AND installation.tenant_id = tr.tenant_id
        WHERE tr.ci_token_id IS NOT NULL
    ) candidate ON TRUE
    GROUP BY r.organization_id, r.repository_id
), eligible_roots AS (
    SELECT
        organization_id,
        min(tenant_id) AS tenant_id,
        (array_agg(github_repository_id ORDER BY repository_id))[1]
            AS github_repository_id,
        (array_agg(ci_token_id ORDER BY repository_id))[1]
            AS ci_token_id
    FROM repository_candidates
    GROUP BY organization_id
    HAVING bool_and(candidate_count = 1)
       AND count(DISTINCT tenant_id) = 1
)
INSERT INTO tenant_operational_roots (
    organization_id,
    tenant_id,
    mapping_basis,
    source_github_repository_id,
    source_ci_token_id,
    verified_at
)
SELECT
    organization_id,
    tenant_id,
    'ci_token_binding',
    github_repository_id,
    ci_token_id,
    now()
FROM eligible_roots;
