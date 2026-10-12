"""Generate a VAPID key pair for web push. Run once, then set the two values as
VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY in Railway (and .env for local dev).

    .venv/bin/python scripts/gen_vapid.py

Prints base64url keys: the public key is the browser's applicationServerKey (an
uncompressed P-256 point); the private key is the raw 32-byte scalar pywebpush accepts.
Regenerating invalidates every existing push subscription.
"""
import base64

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


key = ec.generate_private_key(ec.SECP256R1())
private = key.private_numbers().private_value.to_bytes(32, "big")
public = key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
print(f"VAPID_PUBLIC_KEY={b64url(public)}")
print(f"VAPID_PRIVATE_KEY={b64url(private)}")
