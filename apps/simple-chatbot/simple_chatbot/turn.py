"""Short-lived browser TURN credentials."""

import base64
import hashlib
import hmac
import secrets
import time


def temporary_credentials(secret: str, lifetime: int, label: str) -> dict[str, str]:
    username = f"{int(time.time()) + lifetime}:{label}:{secrets.token_hex(8)}"
    credential = base64.b64encode(
        hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
    ).decode()
    return {"username": username, "credential": credential}
