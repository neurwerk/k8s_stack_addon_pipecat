"""coturn REST credentials and init-only secret configuration assembly."""

import base64
import hashlib
import hmac
import os
import secrets
import time
from ipaddress import AddressValueError, IPv4Address
from pathlib import Path


def temporary_credentials(secret: str, lifetime: int, label: str) -> dict[str, str]:
    username = f"{int(time.time()) + lifetime}:{label}:{secrets.token_hex(8)}"
    credential = base64.b64encode(
        hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
    ).decode()
    return {"username": username, "credential": credential}


def main() -> None:
    try:
        pod_ip = IPv4Address(os.environ.get("POD_IP", ""))
    except AddressValueError:
        raise ValueError("POD_IP must be a unicast IPv4 pod address") from None
    if (
        pod_ip.is_unspecified
        or pod_ip.is_loopback
        or pod_ip.is_link_local
        or pod_ip.is_multicast
        or pod_ip.is_reserved
    ):
        raise ValueError("POD_IP must be a unicast IPv4 pod address")
    secret = Path("/credentials/sharedSecret").read_text().strip()
    if not 32 <= len(secret) <= 256 or any(c not in "0123456789abcdef" for c in secret):
        raise ValueError("TURN sharedSecret must be 32-256 lowercase hex characters")
    base = Path("/config/turnserver.conf").read_text()
    target = Path("/run/coturn/turnserver.conf")
    target.write_text(base + f"\nrelay-ip={pod_ip}\nstatic-auth-secret={secret}\n")
    target.chmod(0o440)
