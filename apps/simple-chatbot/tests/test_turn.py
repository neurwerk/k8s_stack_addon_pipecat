"""App browser relay credentials remain separate from server-side ICE."""

import json
import tempfile
import unittest
from pathlib import Path

from simple_chatbot.config import SecretRef, Settings, browser_ice, server_ice


class TurnTests(unittest.TestCase):
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
            self.assertEqual(server_ice(config), explicit)
