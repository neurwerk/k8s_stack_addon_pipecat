"""Managed TURN config and browser-only relay boundaries, without network access."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from neurwerk_pipecat.config import SecretRef, Settings, browser_ice, server_ice
from neurwerk_pipecat.turn import main


class TurnTests(unittest.TestCase):
    def test_relay_uses_pod_ipv4_and_invalid_addresses_never_publish_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for directory in ("credentials", "config", "run/coturn"):
                (root / directory).mkdir(parents=True)
            secret = "a" * 64
            (root / "credentials/sharedSecret").write_text(secret)
            (root / "config/turnserver.conf").write_text("no-udp\nno-tcp\n")
            target = root / "run/coturn/turnserver.conf"
            with patch("neurwerk_pipecat.turn.Path", side_effect=lambda path: root / path[1:]):
                for address in (
                    None,
                    "",
                    "not-an-ip",
                    "2001:db8::1",
                    "10.42.0.7\nexternal-ip=203.0.113.1",
                    "0.0.0.0",
                    "127.0.0.1",
                    "169.254.1.1",
                    "224.0.0.1",
                    "255.255.255.255",
                ):
                    environment = {} if address is None else {"POD_IP": address}
                    with (
                        self.subTest(address=address),
                        patch.dict(os.environ, environment, clear=True),
                    ):
                        with self.assertRaisesRegex(ValueError, "POD_IP"):
                            main()
                        self.assertFalse(target.exists())
                with patch.dict(os.environ, {"POD_IP": "10.42.0.7"}, clear=True):
                    main()
            lines = target.read_text().splitlines()
            self.assertIn("relay-ip=10.42.0.7", lines)
            self.assertIn("static-auth-secret=" + secret, lines)
            self.assertIn("no-udp", lines)
            self.assertIn("no-tcp", lines)
            self.assertFalse(any(line.startswith("external-ip=") for line in lines))
            self.assertEqual(target.stat().st_mode & 0o777, 0o440)

    def test_managed_turn_is_browser_only_and_explicit_server_ice_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret = root / "turn"
            secret.write_text("a" * 64)
            config = Settings(
                bot_id="example",
                url_slug="example",
                public_origin="https://voice.example.com",
                anonymous=True,
                turn_secret=SecretRef(file=str(secret)),
                turn_urls=["turns:turn.example.com:443?transport=tcp"],
            )
            self.assertEqual(server_ice(config), [])
            browser = browser_ice(config)
            self.assertEqual(len(browser), 1)
            self.assertEqual(browser[0]["urls"], config.turn_urls)
            self.assertIn(":example:", browser[0]["username"])
            self.assertTrue(browser[0]["credential"])
            explicit = [{"urls": "turns:explicit.example.com:443?transport=tcp"}]
            config.ice_servers = explicit
            self.assertEqual(server_ice(config), explicit)
            ice_file = root / "ice.json"
            ice_file.write_text(json.dumps(explicit))
            config.server_ice_secret = SecretRef(file=str(ice_file))
            config.browser_ice_secret = SecretRef(file=str(ice_file))
            self.assertEqual(server_ice(config), explicit)
            self.assertEqual(browser_ice(config)[0], explicit[0])
            self.assertEqual(browser_ice(config)[1]["urls"], config.turn_urls)
            secret.unlink()
            # Server ICE does not even read the managed browser relay credential.
            self.assertEqual(server_ice(config), explicit)
