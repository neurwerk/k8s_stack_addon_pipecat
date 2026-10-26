"""coturn REST credentials and init-only secret configuration assembly."""

import base64
import hashlib
import hmac
import secrets
import time
from pathlib import Path


def temporary_credentials(secret: str, lifetime: int, label: str) -> dict[str, str]:
    username = f"{int(time.time()) + lifetime}:{label}:{secrets.token_hex(8)}"
    credential = base64.b64encode(
        hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
    ).decode()
    return {"username": username, "credential": credential}


def main() -> None:
    secret = Path("/credentials/sharedSecret").read_text().strip()
    if not 32 <= len(secret) <= 256 or any(c not in "0123456789abcdef" for c in secret):
        raise ValueError("TURN sharedSecret must be 32-256 lowercase hex characters")
    base = Path("/config/turnserver.conf").read_text()
    target = Path("/run/coturn/turnserver.conf")
    target.write_text(base + "\nstatic-auth-secret=" + secret + "\n")
    target.chmod(0o440)
