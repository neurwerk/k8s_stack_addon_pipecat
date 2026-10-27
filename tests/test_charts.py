"""Render the actual runtime and Base provisioner to protect their SecretRef boundary."""

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE = Path(os.environ["BASE"]).resolve()


class OIDCSecretReferenceTests(unittest.TestCase):
    def check_references(self, managed: bool) -> None:
        values = yaml.safe_load((ROOT / "validation/values.yaml").read_text())
        values["pipecat"]["secretSync"]["enabled"] = managed
        expected = (
            {"name": "pipecat-oidc-secret", "key": "clientSecret"}
            if managed
            else {"name": "existing-browser-oidc", "key": "clientCredential"}
        )
        values["pipecat"]["oidc"].update(secretName=expected["name"], secretKey=expected["key"])
        values["pipecat"]["oidc"]["gatewayPermissions"] = ["llm:invoke"]
        # Logout requests from broad inputs must also remain subject to trusted approval.
        values["authentication"] = {"postLogoutRedirectUris": ["https://voice.example.com/out"]}

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)

            def render(
                chart: Path, namespace: str, inputs: list[dict], error: str = ""
            ) -> list[dict]:
                command = ["helm", "template", "oidc-check", str(chart), "--namespace", namespace]
                for index, data in enumerate(inputs):
                    path = directory / f"values-{index}.yaml"
                    path.write_text(yaml.safe_dump(data))
                    command += ["--values", str(path)]
                result = subprocess.run(command, check=False, capture_output=True, text=True)
                if error:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(error, result.stderr)
                    return []
                self.assertEqual(result.returncode, 0, result.stderr)
                return [item for item in yaml.safe_load_all(result.stdout) if item]

            settings = render(ROOT / "charts/pipecat/oidc", "auth-keycloak", [values])
            generated = yaml.safe_load(
                next(item for item in settings if item["kind"] == "ConfigMap")["data"][
                    "values.yaml"
                ]
            )
            release = yaml.safe_load((ROOT / "releases/pipecat/setup/oidc.yaml").read_text())
            sources = {
                "auth-keycloak-app-defaults": {
                    "authKeycloak": {"realm": "example", "adminUser": "admin"}
                },
                "client-values": copy.deepcopy(values),
                # An unrelated broad input cannot replace the product's resolved SecretRef.
                "keycloak-product-values": {
                    "clientSecret": {"name": "unrelated", "key": "unrelated"}
                },
                "pipecat-oidc-approval-values": {
                    "clientApproval": {
                        "clientId": "pipecat",
                        "realmRoles": ["pipecat-user"],
                        "audiences": ["agentgateway"],
                        "gatewayPermissions": ["llm:invoke"],
                        "redirectUris": [
                            "https://voice.example.com/simple_chatbot/auth/callback",
                            "https://voice.example.com/public-example/auth/callback",
                            "https://voice.example.com/not-ready/auth/callback",
                        ],
                        "webOrigins": ["https://voice.example.com"],
                        "postLogoutRedirectUris": ["https://voice.example.com/out"],
                    }
                },
                "pipecat-oidc-settings": generated,
            }
            inputs = [sources[reference["name"]] for reference in release["spec"]["valuesFrom"]]
            inputs.append(release["spec"].get("values", {}))
            provisioner = render(BASE / "charts/keycloak/addon-oidc", "auth-keycloak", inputs)
            job = next(item for item in provisioner if item["kind"] == "Job")
            container = job["spec"]["template"]["spec"]["containers"][0]
            credential = next(
                item for item in container["env"] if item["name"] == "KC_CLIENT_SECRET"
            )
            env = {entry["name"]: entry for entry in container["env"]}
            self.assertEqual(
                json.loads(env["KC_APPROVED_REDIRECT_URIS"]["value"]),
                [
                    "https://voice.example.com/simple_chatbot/auth/callback",
                    "https://voice.example.com/public-example/auth/callback",
                    "https://voice.example.com/not-ready/auth/callback",
                ],
            )
            self.assertEqual(
                json.loads(env["KC_APPROVED_POST_LOGOUT_REDIRECT_URIS"]["value"]),
                ["https://voice.example.com/out"],
            )
            if managed:
                # Broad inputs may request access, but cannot approve themselves. Render
                # the real release order against Base's production approval checks.
                for field in (
                    "redirectUris",
                    "webOrigins",
                    "postLogoutRedirectUris",
                    "realmRoles",
                    "audiences",
                    "gatewayPermissions",
                ):
                    with self.subTest(unapproved=field):
                        denied = copy.deepcopy(sources)
                        denied["keycloak-product-values"]["clientApproval"] = copy.deepcopy(
                            sources["pipecat-oidc-approval-values"]["clientApproval"]
                        )
                        denied["pipecat-oidc-approval-values"]["clientApproval"][field] = (
                            ["https://other.example.com/out"]
                            if field == "postLogoutRedirectUris"
                            else []
                        )
                        denied_inputs = [
                            denied[reference["name"]] for reference in release["spec"]["valuesFrom"]
                        ]
                        denied_inputs.append(release["spec"].get("values", {}))
                        render(
                            BASE / "charts/keycloak/addon-oidc",
                            "auth-keycloak",
                            denied_inputs,
                            error=f"authentication {field} require exact client approval",
                        )

            runtime = render(ROOT / "charts/pipecat/app", "pipecat", [values])
            deployment = next(
                item
                for item in runtime
                if item["kind"] == "Deployment" and item["metadata"]["name"] == "pipecat-example"
            )
            volume = next(
                item
                for item in deployment["spec"]["template"]["spec"]["volumes"]
                if item["name"] == "oidc"
            )
            self.assertEqual(credential["valueFrom"]["secretKeyRef"], expected)
            self.assertEqual(volume["secret"]["secretName"], expected["name"])
            self.assertEqual(volume["secret"]["items"], [{"key": expected["key"], "path": "value"}])
            self.assertEqual(job["metadata"]["namespace"], "auth-keycloak")
            self.assertEqual(deployment["metadata"]["namespace"], "pipecat")
            config = next(
                item
                for item in runtime
                if item["kind"] == "ConfigMap" and item["metadata"]["name"] == "pipecat-example"
            )
            self.assertEqual(
                json.loads(config["data"]["config.json"])["oidc"]["client_secret"],
                {"file": "/credentials/oidc/value"},
            )

            for namespace, mode in (("pipecat", {}), ("auth-keycloak", {"identityOnly": True})):
                delivered = render(ROOT / "charts/pipecat/shared", namespace, [values, mode])
                if not managed:
                    self.assertFalse(
                        any(item["kind"] in ("Secret", "ExternalSecret") for item in delivered)
                    )
                else:
                    target = next(
                        item
                        for item in delivered
                        if item["kind"] == "ExternalSecret"
                        and item["metadata"]["name"] == expected["name"]
                    )
                    self.assertEqual(target["metadata"]["namespace"], namespace)
                    self.assertEqual(
                        target["spec"]["data"],
                        [
                            {
                                "secretKey": expected["key"],
                                "remoteRef": {
                                    "key": "addons/pipecat/oidc",
                                    "property": "clientSecret",
                                },
                            }
                        ],
                    )

    def test_custom_unmanaged_reference_agrees_without_copying_credentials(self):
        self.check_references(managed=False)

    def test_canonical_managed_reference_agrees_with_scoped_delivery(self):
        self.check_references(managed=True)
