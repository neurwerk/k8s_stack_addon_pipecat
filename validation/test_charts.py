"""Focused rendered deployment behavior; run with unittest discover -s validation."""

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


class DeploymentBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.values = yaml.safe_load((ROOT / "validation/values.yaml").read_text())

    def render(self, chart, values=None, namespace="pipecat", error=None):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as stream:
            yaml.safe_dump(values or self.values, stream)
            stream.flush()
            result = subprocess.run(
                [
                    "helm",
                    "template",
                    "pipecat",
                    str(ROOT / "charts/pipecat" / chart),
                    "--namespace",
                    namespace,
                    "-f",
                    stream.name,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        if error:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(error, result.stderr)
            return []
        self.assertEqual(result.returncode, 0, result.stderr)
        return [item for item in yaml.safe_load_all(result.stdout) if item]

    def test_disable_removes_only_selected_runtime_and_retains_shared_state(self):
        self.values["pipecat"]["scriptStorage"] = {
            "enabled": True,
            "store": "objects",
            "bucket": "voice-scripts",
        }
        before = self.render("app")
        shared = self.render("shared")
        identity = self.render("oidc", namespace="auth-keycloak")
        self.values["pipecat"]["bots"][1]["enabled"] = False
        after = self.render("app")
        expected = [item for item in before if item["metadata"]["name"] != "pipecat-public-example"]
        self.assertEqual(after, expected)
        self.assertEqual(self.render("shared"), shared)
        self.assertEqual(self.render("oidc", namespace="auth-keycloak"), identity)
        for namespace, mode in (
            ("auth-keycloak", "identityOnly"),
            ("auth-keycloak-api-key-bridge", "managedKeysOnly"),
        ):
            enabled = copy.deepcopy(self.values)
            enabled["pipecat"]["bots"][1]["enabled"] = True
            enabled[mode] = True
            disabled = copy.deepcopy(enabled)
            disabled["pipecat"]["bots"][1]["enabled"] = False
            self.assertEqual(
                self.render("shared", enabled, namespace),
                self.render("shared", disabled, namespace),
            )

    def test_s3_revision_changes_only_own_pod_template_and_uses_rook_keys(self):
        script = {
            "enabled": True,
            "endpoint": "https://s3.example.com",
            "bucket": "voice-scripts",
            "key": "example/v1.tar.gz",
            "sha256": "a" * 64,
            "secretName": "rook-ceph-object-user-objects-pipecat-script-reader",
            "accessKeyKey": "AccessKey",
            "secretKeyKey": "SecretKey",
        }
        self.values["pipecat"]["bots"][0]["script"] = script

        def templates():
            return {
                item["metadata"]["name"]: item["spec"]["template"]
                for item in self.render("app")
                if item["kind"] == "Deployment"
            }

        before = templates()
        env = before["pipecat-example"]["spec"]["initContainers"][0]["env"]
        refs = {
            item["name"]: item["valueFrom"]["secretKeyRef"] for item in env if "valueFrom" in item
        }
        self.assertEqual(refs["AWS_ACCESS_KEY_ID"]["key"], "AccessKey")
        self.assertEqual(refs["AWS_SECRET_ACCESS_KEY"]["key"], "SecretKey")
        for field, value in (("key", "example/v2.tar.gz"), ("sha256", "b" * 64)):
            with self.subTest(field=field):
                original = script[field]
                script[field] = value
                after = templates()
                self.assertNotEqual(before.pop("pipecat-example"), after.pop("pipecat-example"))
                self.assertEqual(before, after)
                script[field] = original
                before = templates()

    def test_gateway_base_url_propagates_and_restarts_selected_bot(self):
        before = self.render("app")
        url = "https://gateway.example.com/custom/voice/v1"
        self.values["pipecat"]["bots"][0]["gatewayBaseUrl"] = url
        after = self.render("app")
        config = next(
            item
            for item in after
            if item["kind"] == "ConfigMap" and item["metadata"]["name"] == "pipecat-example"
        )
        self.assertEqual(json.loads(config["data"]["config.json"])["gateway_base_url"], url)

        def pods(items):
            return [item["spec"]["template"] for item in items if item["kind"] == "Deployment"]

        self.assertNotEqual(pods(before)[0], pods(after)[0])
        self.assertEqual(pods(before)[1:], pods(after)[1:])

    def test_anonymous_bots_still_require_oidc_integration(self):
        self.values["pipecat"]["bots"] = [self.values["pipecat"]["bots"][1]]
        settings = self.render("oidc", namespace="auth-keycloak")[0]
        self.assertTrue(yaml.safe_load(settings["data"]["values.yaml"])["enabled"])
        self.assertTrue(
            any(item["metadata"]["name"] == "pipecat-oidc-secret" for item in self.render("shared"))
        )
        self.values["pipecat"]["oidc"]["enabled"] = False
        for chart in ("app", "oidc", "shared"):
            self.render(chart, error="All bots require OIDC integration")
