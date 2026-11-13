"""Signed 12-hour approvals. Keep this private key on the store's persistent disk."""

import base64
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import time
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from services import db as database

GRACE_SECONDS = 12 * 60 * 60


def canonical(approval):
    return json.dumps(
        approval, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


@lru_cache(maxsize=8)
def _private(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as output:
                output.write(pem)
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise ValueError("Invalid license signing key")
    return key


def private_key():
    return _private(
        str((database.DB_PATH.parent / "license_approval_key.pem").resolve())
    )


def public_key():
    numbers = private_key().public_key().public_numbers()
    return dict(n=str(numbers.n), e=numbers.e)


def sign_lease(key, funpay_id, now=None):
    issued = int(time.time() if now is None else now)
    approval = dict(
        v=1,
        product="autostars",
        fingerprint=hashlib.sha256(key.encode()).hexdigest(),
        funpay_id=str(funpay_id),
        issued_at=issued,
        expires_at=issued + GRACE_SECONDS,
    )
    signature = private_key().sign(
        canonical(approval), padding.PKCS1v15(), hashes.SHA256()
    )
    return dict(
        approval=approval, signature=base64.b64encode(signature).decode("ascii")
    )
