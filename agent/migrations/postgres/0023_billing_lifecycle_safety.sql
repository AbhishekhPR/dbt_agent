-- Durable, tenant-scoped controls for Polar reconciliation, revocation, and
-- checkout recovery. No table here is an entitlement source: tenant_billing
-- remains writable only from verified subscription webhooks.
SET lock_timeout = '5s';
SET statement_timeout = '30s';

CREATE TABLE IF NOT EXISTS tenant_lifecycle_controls (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants (tenant_id) ON DELETE RESTRICT,
    billing_checkout_state TEXT NOT NULL DEFAULT 'active'
        CHECK (billing_checkout_state IN ('active', 'blocked')),
    credential_state TEXT NOT NULL DEFAULT 'active'
        CHECK (credential_state IN ('active', 'revoking', 'revoked')),
    generation BIGINT NOT NULL DEFAULT 0 CHECK (generation >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO tenant_lifecycle_controls
    (tenant_id, billing_checkout_state, credential_state)
SELECT tenant_id, 'active', 'active' FROM tenants
ON CONFLICT (tenant_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS billing_lifecycle_operations (
    operation_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (tenant_id) ON DELETE RESTRICT,
    operation_kind TEXT NOT NULL CHECK (operation_kind IN ('reconcile', 'revoke')),
    state TEXT NOT NULL CHECK (state IN
        ('claimed', 'provider_calls', 'verified_safe', 'failed', 'ambiguous')),
    generation BIGINT NOT NULL CHECK (generation > 0),
    lease_expires_at TIMESTAMPTZ,
    failure_category TEXT,
    subscription_count INTEGER NOT NULL DEFAULT 0 CHECK (subscription_count >= 0),
    actionable_checkout_count INTEGER NOT NULL DEFAULT 0
        CHECK (actionable_checkout_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    UNIQUE (tenant_id, generation),
    CHECK (failure_category IS NULL OR length(failure_category) BETWEEN 1 AND 64)
);

CREATE INDEX IF NOT EXISTS idx_billing_lifecycle_operations_tenant
    ON billing_lifecycle_operations (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS tenant_polar_subscriptions (
    polar_subscription_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (tenant_id) ON DELETE RESTRICT,
    polar_customer_id TEXT NOT NULL,
    polar_product_id TEXT,
    provider_status TEXT NOT NULL,
    cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
    classification TEXT NOT NULL CHECK (classification IN
        ('potentially_billable', 'scheduled_cancel', 'terminal', 'unknown')),
    last_operation_id TEXT NOT NULL REFERENCES billing_lifecycle_operations
        (operation_id) ON DELETE RESTRICT,
    observed_at TIMESTAMPTZ NOT NULL,
    CHECK (length(polar_subscription_id) BETWEEN 1 AND 255),
    CHECK (length(polar_customer_id) BETWEEN 1 AND 255),
    CHECK (polar_product_id IS NULL OR length(polar_product_id) BETWEEN 1 AND 255),
    CHECK (length(provider_status) BETWEEN 1 AND 64)
);

CREATE INDEX IF NOT EXISTS idx_tenant_polar_subscriptions_tenant
    ON tenant_polar_subscriptions (tenant_id, classification);

CREATE TABLE IF NOT EXISTS tenant_polar_checkouts (
    polar_checkout_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (tenant_id) ON DELETE RESTRICT,
    polar_customer_id TEXT,
    provider_status TEXT NOT NULL,
    checkout_intent_id TEXT,
    expires_at TIMESTAMPTZ,
    actionable BOOLEAN NOT NULL,
    last_operation_id TEXT NOT NULL REFERENCES billing_lifecycle_operations
        (operation_id) ON DELETE RESTRICT,
    observed_at TIMESTAMPTZ NOT NULL,
    CHECK (length(polar_checkout_id) BETWEEN 1 AND 255),
    CHECK (polar_customer_id IS NULL OR length(polar_customer_id) BETWEEN 1 AND 255),
    CHECK (length(provider_status) BETWEEN 1 AND 64),
    CHECK (checkout_intent_id IS NULL OR length(checkout_intent_id) BETWEEN 1 AND 255)
);

CREATE INDEX IF NOT EXISTS idx_tenant_polar_checkouts_tenant
    ON tenant_polar_checkouts (tenant_id, actionable);

CREATE TABLE IF NOT EXISTS billing_checkout_intents (
    checkout_intent_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (tenant_id) ON DELETE RESTRICT,
    requested_plan TEXT NOT NULL CHECK (requested_plan IN ('starter', 'pro')),
    polar_product_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN
        ('claimed', 'provider_created', 'completed', 'failed', 'ambiguous')),
    polar_checkout_id TEXT,
    failure_category TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CHECK (length(checkout_intent_id) BETWEEN 1 AND 255),
    CHECK (length(polar_product_id) BETWEEN 1 AND 255),
    CHECK (polar_checkout_id IS NULL OR length(polar_checkout_id) BETWEEN 1 AND 255),
    CHECK (failure_category IS NULL OR length(failure_category) BETWEEN 1 AND 64)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_billing_checkout_intents_active_tenant
    ON billing_checkout_intents (tenant_id)
    WHERE state IN ('claimed', 'provider_created');

