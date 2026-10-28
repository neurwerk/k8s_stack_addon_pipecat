"""Managed TURN config and browser-only relay boundaries, without network access."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
