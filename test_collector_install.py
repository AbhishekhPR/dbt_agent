"""Everything a first design-partner installation depends on.

Three separable concerns, tested separately:

  * dbt relation resolution - does a collection request identify the real
    physical PostgreSQL relation, for manifests dbt actually produces?
  * token lifecycle - can an operator issue, identify and revoke a collector
    credential without the secret ever being recoverable?
  * network configuration - does the customer-facing configuration surface
    refuse the unsafe options and hide the secret ones?

The relation-resolution fixtures are the important ones. dbt names the
physical object differently by resource type, and a collector sent to a table
that does not exist reports it absent from production, which decides BLOCK.
"""
from __future__ import annotations

import json
import os
import unittest
import uuid

from agent.api.auth import (
    AuthenticationError,
    AuthorizationError,
    ServiceTokenAuthenticator,
    hash_secret,
    split_token,
)
from agent.collector.config import CollectorConfig, CollectorConfigError
from agent.collector.provisioning import (
    ProvisioningError,
    issue_collector_token,
    list_collector_tokens,
    revoke_collector_token,
    token_id_of,
)
from agent.collector.signals import UnsafeIdentifierError, split_relation
from agent.metadata_evidence.collection_plan import build_collection_plan

DSN = os.environ.get("RELIUM_TEST_POSTGRES_DSN")

DB = "analytics"


# ------------------------------------------------- dbt manifest fixtures

def _model(name, *, schema="analytics", alias=None, deps=(), cols=("order_id",)):
    """A dbt model node as dbt emits it: `alias` is the physical identifier."""
    resolved = alias or name
    return {"resource_type": "model", "name": name, "schema": schema,
            "alias": resolved, "database": DB,
            "relation_name": f'"{DB}"."{schema}"."{resolved}"',
            "depends_on": {"nodes": list(deps)},
            "columns": {c: {"name": c} for c in cols}}


def _source(name, *, schema="raw", identifier=None,
            cols=("order_id", "discount_amount")):
    """A dbt source node: `identifier` is the physical table, `name` is not."""
    resolved = identifier or name
    return {"resource_type": "source", "name": name, "schema": schema,
            "database": DB, "identifier": resolved,
            "relation_name": f'"{DB}"."{schema}"."{resolved}"',
            "columns": {c: {"name": c} for c in cols}}


def _plan_targets(source_node=None, *, head_model=None, base_model=None):
    sources = {"source.a.raw.orders": source_node or _source("orders")}
    base = {"nodes": {"model.a.fct_orders": base_model or _model(
        "fct_orders", deps=["source.a.raw.orders"])}, "sources": sources}
    head = {"nodes": {"model.a.fct_orders": head_model or _model(
        "fct_orders", deps=["source.a.raw.orders"],
        cols=("order_id", "discount_amount"))}, "sources": sources}
    return build_collection_plan(
        base_manifest=base, head_manifest=head, changed_models=["fct_orders"],
        evidence_level="profile").as_dict()["targets"]


class DbtRelationResolutionTests(unittest.TestCase):
    """Does the request name the relation that actually exists in the warehouse?"""

    def _external(self, source_node):
        targets = _plan_targets(source_node)
        external = [t for t in targets if t["dependency_kind"] == "external"]
        self.assertTrue(external, "expected an external production dependency")
        return external[0]

    def test_default_source_name(self):
        target = self._external(_source("orders", schema="raw"))
        self.assertEqual(target["relation_name"], "raw.orders")
        self.assertEqual(target["relation_schema"], "raw")

    def test_source_with_a_custom_identifier(self):
        """dbt sources carry `identifier`; `name` is only the logical handle.

        Reading `alias or name` sent the collector to raw.orders when the real
        table was raw.orders_raw_v2 - reported absent, decided BLOCK.
        """
        target = self._external(_source("orders", schema="raw",
                                        identifier="orders_raw_v2"))
        self.assertEqual(target["relation_name"], "raw.orders_raw_v2")

    def test_source_in_a_custom_schema(self):
        target = self._external(_source("orders", schema="raw_prod_eu"))
        self.assertEqual(target["relation_name"], "raw_prod_eu.orders")
        self.assertEqual(target["relation_schema"], "raw_prod_eu")

    def test_custom_schema_and_custom_identifier_together(self):
        target = self._external(_source("orders", schema="raw_prod_eu",
                                        identifier="orders_raw_v2"))
        self.assertEqual(target["relation_name"], "raw_prod_eu.orders_raw_v2")
        self.assertEqual(target["relation_schema"], "raw_prod_eu")

    def test_model_alias_is_the_physical_relation(self):
        targets = _plan_targets(
            head_model=_model("fct_orders", schema="marts", alias="fact_orders",
                              deps=["source.a.raw.orders"],
                              cols=("order_id", "discount_amount")),
            base_model=_model("fct_orders", schema="marts", alias="fact_orders",
                              deps=["source.a.raw.orders"]))
        names = {t["relation_name"] for t in targets}
        self.assertIn("marts.fact_orders", names)
        self.assertNotIn("marts.fct_orders", names,
                         "the model name is not the physical relation")

    def test_request_carries_the_schema_needed_to_resolve_physically(self):
        target = self._external(_source("orders", schema="raw_prod_eu",
                                        identifier="orders_raw_v2"))
        for field in ("relation_name", "relation_schema", "relation_database"):
            with self.subTest(field=field):
                self.assertTrue(target[field], f"{field} is required to resolve")


class RelationSplittingTests(unittest.TestCase):
    def test_authoritative_schema_beats_naive_dot_splitting(self):
        self.assertEqual(
            split_relation("raw_prod_eu.orders_raw_v2",
                           relation_schema="raw_prod_eu"),
            ("raw_prod_eu", "orders_raw_v2"))

    def test_falls_back_when_the_request_carries_no_schema(self):
        self.assertEqual(split_relation("raw.orders"), ("raw", "orders"))
        self.assertEqual(split_relation("orders"), ("public", "orders"))

    def test_still_refuses_unsafe_identifiers(self):
        for bad in ("orders; drop table orders", 'x" --', "a.b.c"):
            with self.subTest(name=bad):
                with self.assertRaises(UnsafeIdentifierError):
                    split_relation(bad)


# -------------------------------------------------------- configuration

class NetworkConfigurationTests(unittest.TestCase):
    def _env(self, **overrides):
        env = {"RELIUM_API_URL": "https://relium.example.com",
               "RELIUM_API_TOKEN": "rlm_abc.super-secret-value",
               "RELIUM_WAREHOUSE_DSN":
                   "postgresql://ro:pw@wh.internal:5432/analytics?sslmode=require",
               "RELIUM_ENVIRONMENT": "production"}
        env.update(overrides)
        return {k: v for k, v in env.items() if v is not None}

    def test_plaintext_api_url_is_refused_for_a_remote_host(self):
        with self.assertRaises(CollectorConfigError) as caught:
            CollectorConfig.from_env(self._env(RELIUM_API_URL="http://relium.example.com"))
        self.assertIn("https", str(caught.exception))

    def test_loopback_over_http_is_allowed_for_local_verification(self):
        config = CollectorConfig.from_env(
            self._env(RELIUM_API_URL="http://127.0.0.1:8799"))
        self.assertEqual(config.api_url, "http://127.0.0.1:8799")

    def test_there_is_no_setting_that_disables_tls_verification(self):
        import inspect

        from agent.collector import client, config as config_module

        for module in (client, config_module):
            source = inspect.getsource(module)
            with self.subTest(module=module.__name__):
                self.assertNotIn("CERT_NONE", source)
                self.assertNotIn("check_hostname = False", source)
                self.assertNotIn("verify=False", source)

    def test_missing_ca_bundle_is_refused_rather_than_ignored(self):
        with self.assertRaises(CollectorConfigError):
            CollectorConfig.from_env(
                self._env(RELIUM_API_CA_BUNDLE="/no/such/bundle.pem"))

    def test_statement_timeout_is_configurable(self):
        config = CollectorConfig.from_env(self._env(RELIUM_STATEMENT_TIMEOUT_MS="5000"))
        self.assertEqual(config.statement_timeout_ms, 5000)

    def test_config_never_renders_the_token_or_the_dsn(self):
        config = CollectorConfig.from_env(self._env())
        for rendering in (repr(config), str(config), f"{config}"):
            self.assertNotIn("super-secret-value", rendering)
            self.assertNotIn("pw@", rendering)
            self.assertNotIn("sslmode=require", rendering)

    def test_safe_warehouse_keeps_host_and_database_only(self):
        config = CollectorConfig.from_env(self._env())
        self.assertEqual(config.safe_warehouse, "wh.internal:5432/analytics")
        self.assertNotIn("ro", config.safe_warehouse.split("/")[0].split(":")[0])

    def test_missing_configuration_names_what_is_missing_and_nothing_else(self):
        with self.assertRaises(CollectorConfigError) as caught:
            CollectorConfig.from_env({"RELIUM_API_URL": "https://relium.example.com"})
        message = str(caught.exception)
        self.assertIn("RELIUM_API_TOKEN", message)
        self.assertIn("RELIUM_WAREHOUSE_DSN", message)


# ------------------------------------------------------ token lifecycle

@unittest.skipUnless(DSN, "RELIUM_TEST_POSTGRES_DSN not set")
class CollectorTokenLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from agent.postgres_lifecycle_store import PostgresLifecycleStore

        cls.store = PostgresLifecycleStore(DSN)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def setUp(self):
        unique = uuid.uuid4().hex[:8]
        self.org = f"org-{unique}"
        self.repo = f"repo-{unique}"
        self.env = "production"
        self.auth = ServiceTokenAuthenticator(self.store)

    def _issue(self, **overrides):
        kwargs = dict(organization_id=self.org, repository_id=self.repo,
                      environment=self.env, description="design partner")
        kwargs.update(overrides)
        return issue_collector_token(self.store, **kwargs)

    def test_generated_token_authenticates_to_the_right_tenant(self):
        _, presented = self._issue()
        scope = self.auth.authenticate(presented)
        self.assertEqual(scope.organization_id, self.org)
        self.assertEqual(scope.repository_id, self.repo)
        self.assertEqual(scope.environment, self.env)

    def test_token_uses_the_existing_presentation_format(self):
        token_id, presented = self._issue()
        self.assertTrue(presented.startswith("rlm_"))
        self.assertEqual(split_token(presented)[0], token_id)

    def test_wrong_environment_is_denied(self):
        _, presented = self._issue()
        scope = self.auth.authenticate(presented)
        with self.assertRaises(AuthorizationError):
            scope.require_environment("staging")

    def test_a_token_cannot_reach_another_tenant(self):
        _, presented = self._issue()
        scope = self.auth.authenticate(presented)
        self.assertNotEqual(scope.organization_id, "some-other-org")
        self.assertFalse(scope.permits_environment("staging"))

    def test_revoked_token_fails_authentication(self):
        token_id, presented = self._issue()
        self.assertTrue(self.auth.authenticate(presented))
        self.assertTrue(revoke_collector_token(self.store, token_id))
        with self.assertRaises(AuthenticationError):
            self.auth.authenticate(presented)

    def test_revoking_twice_reports_honestly(self):
        token_id, _ = self._issue()
        self.assertTrue(revoke_collector_token(self.store, token_id))
        self.assertFalse(revoke_collector_token(self.store, token_id),
                         "a second revoke did nothing and must say so")

    def test_only_a_hash_is_persisted(self):
        token_id, presented = self._issue()
        secret = presented.split(".", 1)[1]
        row = self.store.get_service_token(token_id)
        self.assertEqual(row["secret_hash"], hash_secret(secret))
        self.assertNotIn(secret, json.dumps(row, default=str))

    def test_the_plaintext_secret_cannot_be_recovered(self):
        token_id, presented = self._issue()
        secret = presented.split(".", 1)[1]
        listed = json.dumps(list_collector_tokens(self.store,
                                                  organization_id=self.org),
                            default=str)
        self.assertNotIn(secret, listed)
        self.assertNotIn(presented, listed)
        # Nor from the raw row, which carries only the digest.
        self.assertNotIn(secret,
                         json.dumps(self.store.get_service_token(token_id),
                                    default=str))

    def test_listing_identifies_a_token_without_exposing_it(self):
        token_id, _ = self._issue(description="acme prod collector")
        rows = list_collector_tokens(self.store, organization_id=self.org)
        row = next(r for r in rows if r["token_id"] == token_id)
        self.assertEqual(row["description"], "acme prod collector")
        self.assertEqual(row["environment"], self.env)
        self.assertIsNone(row["revoked_at"])
        self.assertNotIn("secret_hash", row,
                         "even the digest need not leave the store")

    def test_a_holder_can_be_matched_to_a_row_without_comparing_secrets(self):
        token_id, presented = self._issue()
        self.assertEqual(token_id_of(presented), token_id)

    def test_an_unscoped_environment_is_refused(self):
        with self.assertRaises(ProvisioningError):
            self._issue(environment=None)

    def test_token_values_never_appear_in_ordinary_logs(self):
        import logging

        _, presented = self._issue()
        secret = presented.split(".", 1)[1]
        logger = logging.getLogger("relium.collector")
        with self.assertLogs(logger, level="DEBUG") as captured:
            logger.info("collector_configured environment=%s", self.env)
            self.auth.authenticate(presented)
        blob = "\n".join(captured.output)
        self.assertNotIn(secret, blob)
        self.assertNotIn(presented, blob)


if __name__ == "__main__":
    unittest.main()
