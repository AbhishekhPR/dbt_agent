-- Polar billing, bound to the Relium tenant.
--
-- ###################################################################
-- # BILLING BELONGS TO THE WORKSPACE, NEVER TO AN EMAIL ADDRESS.    #
-- ###################################################################
--
-- The tenant is the customer. `tenant_id` is the primary key here, which makes
-- "one workspace has at most one Polar subscription" a database fact rather
-- than an application convention. An email address is a property of a person,
-- it changes, and several people share one workspace — keying a subscription
-- by one would let whoever controls that mailbox move a paid plan between
-- workspaces.
--
-- The same identifier is what Relium sends to Polar as the external customer
-- id, so the association survives in both directions:
--
--   Relium tenant_id  --(external_customer_id on checkout)-->  Polar customer
--   Polar webhook     --(customer.external_id)-->              Relium tenant
--
-- ABSENCE OF A ROW MEANS FREE
-- ---------------------------
-- A tenant with no row here is on the free plan. Nothing has to write a row to
-- make that true, so a failed insert can never silently grant or withhold a
-- paid plan, and existing tenants need no backfill.
--
-- PLAN IS NEVER DERIVED FROM A PRICE
-- ----------------------------------
-- `plan` is one of three stable internal names. It is resolved from the
-- CONFIGURED Polar product ids (POLAR_STARTER_PRODUCT_ID / POLAR_PRO_PRODUCT_ID)
-- and from nothing else. An amount in cents is a fact about an invoice, not an
-- entitlement, and a product this deployment has not been configured with maps
-- to 'free' — see agent/billing/plans.py.

CREATE TABLE IF NOT EXISTS tenant_billing (
    -- The workspace. One row per tenant, and the subscription travels with the
    -- workspace rather than with whoever happened to pay for it.
    tenant_id              TEXT PRIMARY KEY
                           REFERENCES tenants (tenant_id) ON DELETE CASCADE,

    -- Polar's own identifiers. UNIQUE so one Polar customer or subscription
    -- cannot be claimed by two workspaces: if a webhook ever tried to attach an
    -- already-bound subscription to a second tenant, the database refuses
    -- rather than quietly re-pointing a paid plan.
    polar_customer_id      TEXT UNIQUE,
    polar_subscription_id  TEXT UNIQUE,

    -- Which Polar product the subscription is for. Recorded as reported, so an
    -- operator can see what Polar said even when it maps to no configured plan.
    polar_product_id       TEXT,

    -- Relium's stable internal plan name. This, not the product id, is what the
    -- rest of the application reads.
    plan                   TEXT NOT NULL DEFAULT 'free',

    -- Polar's own subscription status vocabulary, stored verbatim. NOT
    -- constrained to a fixed list: Polar owns this enum and may extend it, and
    -- a CHECK that rejected an unrecognised status would turn a new Polar state
    -- into a failed webhook and a permanently stale row. Access is decided from
    -- an explicit allow-list in application code, so an unknown status grants
    -- nothing.
    subscription_status    TEXT,

    current_period_end     TIMESTAMPTZ,
    cancel_at_period_end   BOOLEAN NOT NULL DEFAULT FALSE,

    -- When Polar first reported a failed renewal. Kept because the documented
    -- payment-recovery window is measured from it, and Relium's own grace
    -- period has to be measured from the same instant.
    past_due_at            TIMESTAMPTZ,

    -- The `modified_at` of the Polar subscription this row was last written
    -- from. Webhook deliveries can arrive out of order; an older object must
    -- never overwrite a newer one, and this is what makes that checkable.
    subscription_modified_at TIMESTAMPTZ,

    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT tenant_billing_plan_check
        CHECK (plan IN ('free', 'starter', 'pro')),
    CONSTRAINT tenant_billing_customer_id_bounded
        CHECK (polar_customer_id IS NULL
               OR length(polar_customer_id) BETWEEN 1 AND 255),
    CONSTRAINT tenant_billing_subscription_id_bounded
        CHECK (polar_subscription_id IS NULL
               OR length(polar_subscription_id) BETWEEN 1 AND 255),
    CONSTRAINT tenant_billing_product_id_bounded
        CHECK (polar_product_id IS NULL
               OR length(polar_product_id) BETWEEN 1 AND 255),
    CONSTRAINT tenant_billing_status_bounded
        CHECK (subscription_status IS NULL
               OR length(subscription_status) BETWEEN 1 AND 64),

    -- A paid plan cannot exist without the subscription that justifies it.
    -- Without this, a partial write could leave a workspace on 'pro' with
    -- nothing recorded that Polar could ever revoke.
    CONSTRAINT tenant_billing_paid_plan_has_subscription
        CHECK (plan = 'free' OR polar_subscription_id IS NOT NULL)
);

-- Resolving a webhook to a workspace when the payload carries only Polar's own
-- customer id. The external id is the primary path; this is the fallback for a
-- customer created before an external id was set.
CREATE INDEX IF NOT EXISTS idx_tenant_billing_polar_customer
    ON tenant_billing (polar_customer_id)
    WHERE polar_customer_id IS NOT NULL;


-- Webhook delivery de-duplication.
--
-- ###################################################################
-- # IDEMPOTENCY IS ENFORCED HERE, NOT BY HOPING FOR ONE DELIVERY.   #
-- ###################################################################
--
-- Standard Webhooks gives every message a unique `webhook-id`, and Polar
-- retries on failure. The primary key is that id: a replayed delivery loses the
-- insert and is skipped before it can touch tenant_billing. Together with the
-- `subscription_modified_at` guard above, that covers both duplicates (same
-- message twice) and reordering (an older message after a newer one).
--
-- Only the delivery identity and the event type are kept. The payload is not
-- stored: it carries customer names and email addresses, and nothing in Relium
-- reads it back.
CREATE TABLE IF NOT EXISTS billing_webhook_deliveries (
    delivery_id   TEXT PRIMARY KEY,
    event_type    TEXT NOT NULL,
    tenant_id     TEXT REFERENCES tenants (tenant_id) ON DELETE SET NULL,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT billing_webhook_deliveries_id_bounded
        CHECK (length(delivery_id) BETWEEN 1 AND 255),
    CONSTRAINT billing_webhook_deliveries_event_bounded
        CHECK (length(event_type) BETWEEN 1 AND 128)
);

-- Old deliveries are prunable by age; the index makes that a range scan rather
-- than a table scan.
CREATE INDEX IF NOT EXISTS idx_billing_webhook_deliveries_received
    ON billing_webhook_deliveries (received_at);
