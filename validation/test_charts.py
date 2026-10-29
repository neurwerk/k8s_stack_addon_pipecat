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
        inputs = values if isinstance(values, list) else [values or self.values]
        with tempfile.TemporaryDirectory() as directory:
            command = [
                "helm",
                "template",
                "pipecat",
                str(ROOT / "charts/pipecat" / chart),
                "--namespace",
                namespace,
            ]
            for index, data in enumerate(inputs):
                path = Path(directory) / f"values-{index}.yaml"
                path.write_text(yaml.safe_dump(data))
                command += ["-f", str(path)]
            result = subprocess.run(
                command,
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

    def test_machine_provider_grants_reject_legacy_roles_even_when_approved(self):
        self.values["machineIdentitiesOnly"] = True
        for provider_role in (None, "global-use", "company-use"):
            with self.subTest(provider_role=provider_role):
                roles = ["llm:invoke", "model:example-model:invoke"]
                if provider_role:
                    roles.append(f"provider:example:{provider_role}")
                self.values["authKeycloak"] = {
                    "realm": "example",
                    "adminUser": "admin",
                    "agentgatewayClientId": "agentgateway",
                    "agentgatewayClientRoles": list(roles),
                }
                product = self.values["pipecat"]
                product["bots"][1]["gatewayPermissions"] = list(roles)
                product["machineIdentityApprovals"] = {
                    "public-example": {
                        "clientId": "pipecat-bot-public-example",
                        "realmRoles": [],
                        "gatewayPermissions": list(roles),
                        "audiences": ["agentgateway"],
                    }
                }
                if provider_role == "company-use":
                    self.render(
                        "oidc",
                        namespace="auth-keycloak",
                        error=(
                            "Managed bots may invoke only their canonical model "
                            "and explicitly approved providers"
                        ),
                    )
                    continue
                resources = self.render("oidc", namespace="auth-keycloak")
                job = next(
                    item
                    for item in resources
                    if item["kind"] == "Job"
                    and item["metadata"]["name"] == "pipecat-bot-public-example-oidc"
                )
                env = job["spec"]["template"]["spec"]["containers"][0]["env"]
                scope = next(
                    item["value"] for item in env if item["name"] == "KC_GATEWAY_ROLE_SCOPE"
                )
                self.assertEqual(json.loads(scope), roles)

    def test_turn_exposes_only_tls_and_supplies_private_relay_address(self):
        shared = self.render("shared")
        service = next(item for item in shared if item["kind"] == "Service")
        self.assertEqual(service["spec"]["type"], "NodePort")
        self.assertEqual(service["spec"]["externalTrafficPolicy"], "Local")
        self.assertEqual(
            service["spec"]["ports"],
            [
                {
                    "name": "turn-tls",
                    "protocol": "TCP",
                    "port": 5349,
                    "targetPort": 5349,
                    "nodePort": 30549,
                }
            ],
        )
        deployment = next(item for item in shared if item["kind"] == "Deployment")
        pod = deployment["spec"]["template"]["spec"]
        self.assertEqual(pod["nodeSelector"], {"kubernetes.io/hostname": "relay-example"})
        self.assertEqual(
            pod["initContainers"][0]["env"],
            [{"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}}],
        )
        config = next(item for item in shared if item["kind"] == "ConfigMap")
        lines = config["data"]["turnserver.conf"].splitlines()
        for directive in (
            "no-udp",
            "no-tcp",
            "no-dtls",
            "no-tcp-relay",
            "denied-peer-ip=0.0.0.0-255.255.255.255",
            "denied-peer-ip=::-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
            "allowed-peer-ip=10.42.0.1-10.42.0.254",
        ):
            self.assertIn(directive, lines)
        self.assertNotIn("no-udp-relay", lines)
        self.assertFalse(any(line.startswith("external-ip=") for line in lines))
        for item in self.render("app"):
            if item["kind"] == "ConfigMap":
                self.assertEqual(
                    json.loads(item["data"]["config.json"])["turn_urls"],
                    ["turns:turn.example.com:443?transport=tcp"],
                )

    def test_private_media_routes_are_scoped_to_same_namespace_bot_and_turn_pods(self):
        bot_peer = {
            "podSelector": {
                "matchLabels": {
                    "app.kubernetes.io/name": "pipecat",
                    "app.kubernetes.io/component": "bot",
                }
            }
        }
        turn_peer = {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "pipecat-turn"}}}
        media = {"protocol": "UDP", "port": 1024, "endPort": 65535}
        relay = {"protocol": "UDP", "port": 31000, "endPort": 31015}
        turn_policy = next(
            item for item in self.render("shared") if item["kind"] == "NetworkPolicy"
        )["spec"]
        self.assertEqual(
            turn_policy["ingress"],
            [
                {"ports": [{"protocol": "TCP", "port": 5349}]},
                {"from": [bot_peer], "ports": [relay]},
            ],
        )
        self.assertEqual(turn_policy["egress"], [{"to": [bot_peer], "ports": [media]}])
        app_egress = {
            "to": [{"ipBlock": {"cidr": "192.0.2.1/32"}}],
            "ports": [{"protocol": "TCP", "port": 443}],
        }
        self.values["pipecat"]["egress"] = [app_egress]

        def policy():
            return next(item for item in self.render("app") if item["kind"] == "NetworkPolicy")[
                "spec"
            ]

        enabled = policy()
        self.assertEqual(enabled["ingress"][1:], [{"from": [turn_peer], "ports": [media]}])
        self.assertEqual(
            enabled["egress"][1:],
            [
                {"to": [turn_peer], "ports": [relay]},
                app_egress,
            ],
        )
        self.values["pipecat"]["turn"]["enabled"] = False
        disabled = policy()
        self.assertEqual(disabled["ingress"], enabled["ingress"][:1])
        self.assertEqual(disabled["egress"], [enabled["egress"][0], app_egress])

    def test_turn_requires_explicit_valid_peer_ranges_and_a_pinned_node(self):
        for ranges in (
            [],
            ["0.0.0.0-255.255.255.255"],
            ["127.0.0.1-127.0.0.2"],
            ["10.0.0.1-192.168.0.1"],
            ["10.42.0.254-10.42.0.1"],
        ):
            with self.subTest(ranges=ranges):
                self.values["pipecat"]["turn"]["peerAddressRanges"] = ranges
                self.render(
                    "shared", error="TURN peer ranges" if ranges else "turn.peerAddressRanges"
                )
        self.values["pipecat"]["turn"]["peerAddressRanges"] = ["10.42.0.1-10.42.0.999"]
        self.render("shared", error="TURN peer ranges require valid IPv4 addresses")
        self.values["pipecat"]["turn"]["peerAddressRanges"] = ["10.42.0.1-10.42.0.254"]
        self.values["pipecat"]["turn"]["nodeSelector"] = {}
        self.render("shared", error="turn.nodeSelector must pin the relay")

    def test_certificate_approval_uses_layered_product_turn_hostname(self):
        release = yaml.safe_load(
            (ROOT / "releases/pipecat/setup/certificate-approval.yaml").read_text()
        )
        sources = {
            "client-values": {
                "pipecat": {
                    "enabled": True,
                    "hostname": "voice.example.com",
                    "turn": {"enabled": False, "hostname": ""},
                },
            },
            "pipecat-product-values": {
                "pipecat": {"turn": {"enabled": True, "hostname": "turn.example.com"}},
            },
        }

        def render(error=None):
            inputs = [sources[ref["name"]] for ref in release["spec"]["valuesFrom"]]
            return self.render(
                "certificate-approval", inputs, release["metadata"]["namespace"], error=error
            )

        resources = render()
        policies = {
            item["metadata"]["name"]: item["spec"]
            for item in resources
            if item["kind"] == "CertificateRequestPolicy"
        }
        for suffix in ("staging", "production"):
            for name, hostname in (
                ("pipecat", "voice.example.com"),
                ("pipecat-turn", "turn.example.com"),
            ):
                policy = policies[f"cert-manager-public-{name}-{suffix}"]
                self.assertEqual(
                    policy["allowed"]["dnsNames"], {"values": [hostname], "required": True}
                )
                self.assertEqual(policy["selector"]["namespace"], {"matchNames": ["pipecat"]})
        role = next(item for item in resources if item["kind"] == "ClusterRole")
        self.assertEqual(set(role["rules"][0]["resourceNames"]), set(policies))
        sources["pipecat-product-values"]["pipecat"]["turn"]["hostname"] = ""
        render(error="Application and enabled TURN hostnames are required")
