# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.mail.cluster import plan, telemetry
from suite_cloud.mail.stalwart import schema
from suite_cloud.mail.tests.fixtures import (
    clear_request_cache,
    configure_settings,
    make_cluster,
    make_store,
)
from suite_cloud.mail.tests.test_tenancy import TenancyTestCase
from suite_cloud.tests.fixtures import ROOT_DOMAIN

V1 = "v1.1.1"
LEGACY = "v0.16.27"


def operation(rendered: list[dict], object_type: str) -> dict:
    return next(op for op in rendered if op["object"] == object_type)


class TestPlanSchema(IntegrationTestCase):
    """0.16 and 1.x clusters side by side: each gets the field names of its own release."""

    def setUp(self) -> None:
        frappe.flags.do_not_enqueue = True
        configure_settings(stalwart_version=LEGACY)
        self.legacy = make_cluster("blr-1", stalwart_version=LEGACY)
        self.current = make_cluster("blr-2", hostname=f"mail.blr2.{ROOT_DOMAIN}", stalwart_version=V1)

    def tearDown(self) -> None:
        configure_settings(stalwart_version=None)
        frappe.flags.do_not_enqueue = False

    def test_is_v1(self) -> None:
        self.assertFalse(schema.is_v1(self.legacy))
        self.assertTrue(schema.is_v1(self.current))
        self.assertFalse(schema.is_v1("v0.16.27"))
        self.assertTrue(schema.is_v1("v1.0.0"))
        self.assertFalse(schema.is_v1(None))  # the site's release for new clusters

    def test_bootstrap_plan(self) -> None:
        legacy = plan.bootstrap_plan(self.legacy)[0]["value"]
        current = plan.bootstrap_plan(self.current)[0]["value"]
        self.assertEqual(legacy["serverHostname"], self.legacy.hostname)
        self.assertNotIn("defaultHostname", legacy)
        self.assertEqual(current["defaultHostname"], self.current.hostname)
        self.assertNotIn("serverHostname", current)

        # The PostgreSQL data store: 0.16 has one timeout, 1.x a connect timeout.
        self.assertEqual(legacy["dataStore"]["timeout"], 15000)
        self.assertEqual(current["dataStore"]["connectTimeout"], 15000)
        self.assertNotIn("timeout", current["dataStore"])
        self.assertEqual(legacy["blobStore"]["accessKey"], {"@type": "Value", "value": "AK"})
        self.assertEqual(current["blobStore"]["accessKeyId"], {"@type": "Value", "value": "AK"})
        self.assertNotIn("accessKey", current["blobStore"])
        self.assertEqual(legacy["inMemoryStore"]["timeout"], 10000)
        self.assertEqual(current["inMemoryStore"]["connectTimeout"], 10000)

    def test_cluster_and_recovery_plans(self) -> None:
        legacy = plan.recovery_plan(self.legacy)
        current = plan.recovery_plan(self.current)

        legacy_domain = operation(legacy, "Domain")["value"]["default"]
        current_domain = operation(current, "Domain")["value"]["default"]
        self.assertIs(legacy_domain["isEnabled"], True)
        self.assertNotIn("enable", legacy_domain)
        self.assertIs(current_domain["enable"], True)
        self.assertNotIn("isEnabled", current_domain)

        legacy_listener = operation(legacy, "NetworkListener")["value"]["https"]
        current_listener = operation(current, "NetworkListener")["value"]["https"]
        self.assertIs(legacy_listener["tlsImplicit"], True)
        self.assertIs(current_listener["implicitTls"], True)
        self.assertNotIn("tlsImplicit", current_listener)

        legacy_alias = operation(legacy, "Account")["value"]["admin"]["aliases"]["0"]
        current_alias = operation(current, "Account")["value"]["admin"]["aliases"]["0"]
        self.assertIs(legacy_alias["enabled"], True)
        self.assertIs(current_alias["enable"], True)
        self.assertNotIn("enabled", current_alias)
        pushed_alias = operation(plan.cluster_plan(self.current), "Account")["value"]["admin"]["aliases"]["0"]
        self.assertIs(pushed_alias["enable"], True)

        legacy_milter = operation(plan.cluster_plan(self.legacy), "MtaMilter")
        current_milter = operation(plan.cluster_plan(self.current), "MtaMilter")
        self.assertEqual(legacy_milter["matchOn"], ["hostname", "port"])
        self.assertEqual(current_milter["matchOn"], ["host", "port"])
        legacy_value = legacy_milter["value"]["outbound-scan"]
        current_value = current_milter["value"]["outbound-scan"]
        self.assertEqual(
            [legacy_value[k] for k in ("hostname", "timeoutConnect", "timeoutCommand", "timeoutData")],
            [current_value[k] for k in ("host", "connectTimeout", "commandTimeout", "dataTimeout")],
        )
        self.assertFalse({"hostname", "timeoutConnect", "timeoutCommand", "timeoutData"} & set(current_value))

    def test_telemetry_operations(self) -> None:
        with patch.object(telemetry, "is_push_enabled", return_value=True):
            legacy = telemetry.get_operations(self.legacy)
            current = telemetry.get_operations(self.current)

        legacy_events = operation(legacy, "WebHook")["value"]["telemetry-errors"]["events"]
        current_events = operation(current, "WebHook")["value"]["telemetry-errors"]["events"]
        self.assertIn("dns.build-error", legacy_events)  # 0.16 still emits it
        self.assertNotIn("dns.build-error", current_events)
        for stream in ("telemetry-events", "telemetry-errors", "telemetry-warnings"):
            events = operation(current, "WebHook")["value"][stream]["events"]
            self.assertFalse(set(events) & schema.REMOVED_EVENTS, stream)

        legacy_otlp = operation(legacy, "Metrics")["value"]["openTelemetry"]
        current_otlp = operation(current, "Metrics")["value"]["openTelemetry"]
        self.assertEqual(legacy_otlp["endpoint"], current_otlp["url"])
        self.assertNotIn("endpoint", current_otlp)

    def test_store_variants(self) -> None:
        mysql = make_store("Data", "MySql", host="db.example.test")
        self.assertEqual(mysql.config_for(self.legacy)["timeout"], 15000)
        # MySQL's 1.x idleTimeout is how long an idle connection stays open, not a timeout.
        self.assertFalse({"timeout", "idleTimeout"} & set(mysql.config_for(self.current)))

        endpoint = "https://s3.fr-par.scw.cloud"
        s3 = make_store(
            "Blob", "S3", region="fr-par", endpoint=endpoint, bucket="mail", access_key="AK", secret_key="SK"
        )
        self.assertEqual(
            s3.config_for(self.legacy)["region"],
            {"@type": "Custom", "customEndpoint": endpoint, "customRegion": "fr-par"},
        )
        self.assertEqual(
            s3.config_for(self.current)["region"], {"@type": "Custom", "url": endpoint, "region": "fr-par"}
        )

        meilisearch = make_store("Search", "Meilisearch", url="https://search.example.test")
        polls = meilisearch.config_for(self.legacy)["maxRetries"]
        self.assertEqual(meilisearch.config_for(self.current)["maxPolls"], polls)
        self.assertNotIn("maxRetries", meilisearch.config_for(self.current))

    def test_a_new_cluster_gets_the_plan_of_the_release_it_installs(self) -> None:
        # A site that still names 0.16: a new cluster installs it, and gets the 0.16 plan.
        cluster = make_cluster("blr-3", hostname=f"mail.blr3.{ROOT_DOMAIN}")
        self.assertEqual(cluster.stalwart_version, LEGACY)
        self.assertIn("serverHostname", plan.bootstrap_plan(cluster)[0]["value"])

        # Once the site names 1.x, new clusters get 1.x; the 0.16 ones keep their plan and are not
        # offered an upgrade they cannot take in place.
        configure_settings(stalwart_version=V1)
        clear_request_cache()
        cluster = make_cluster("blr-4", hostname=f"mail.blr4.{ROOT_DOMAIN}")
        self.assertEqual(cluster.stalwart_version, V1)
        self.assertIn("defaultHostname", plan.bootstrap_plan(cluster)[0]["value"])
        self.legacy.reload()
        self.assertIn("serverHostname", plan.bootstrap_plan(self.legacy)[0]["value"])
        self.assertFalse(self.legacy.is_version_behind)


class TestDirectorySchema(TenancyTestCase):
    """Domains and aliases on a 1.x cluster carry its names; the 0.16 ones are in test_tenancy."""

    def setUp(self) -> None:
        super().setUp()
        self.cluster.db_set("stalwart_version", V1)
        frappe.clear_document_cache("Stalwart Cluster", self.cluster.name)

    def test_domain_and_alias_names(self) -> None:
        domain = self.make_domain(is_verified=0)
        live = self.fake.find("Domain", name="acme.com")
        self.assertIs(live["enable"], False)
        self.assertNotIn("isEnabled", live)

        domain.is_verified = 1
        domain.save()
        self.assertIs(self.fake.find("Domain", name="acme.com")["enable"], True)

        account = self.make_account("alice@acme.com", aliases=[{"alias_email": "ally@acme.com"}])
        alias = self.fake.get("Account", account.stalwart_id)["aliases"]["0"]
        self.assertEqual(alias["name"], "ally")
        self.assertIs(alias["enable"], True)
        self.assertNotIn("enabled", alias)
