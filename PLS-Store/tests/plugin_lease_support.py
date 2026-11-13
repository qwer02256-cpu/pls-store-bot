"""Isolated signing fixtures for executing the actual plugin guard functions."""

import ast
import base64
import hashlib
import hmac
import json
import math
import os
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric import rsa
from services import license_leases

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_NUMBERS = _KEY.public_key().public_numbers()
PUBLIC_KEY = {"n": str(_NUMBERS.n), "e": _NUMBERS.e}
GUARD_NAMES = {
    "_pls_clear_memory",
    "_pls_store_status",
    "_pls_verify_approval",
    "_pls_remember",
    "_pls_deny",
    "_pls_cached_approval",
    "_pls_require_license",
}


def payload(key, fid="123", now=1000):
    with patch.object(license_leases, "private_key", return_value=_KEY):
        return dict(valid=True, **license_leases.sign_lease(key, fid, now))


def defaults():
    return dict(
        os=os,
        json=json,
        _pls_base64=base64,
        _pls_hashlib=hashlib,
        _pls_hmac=hmac,
        _pls_math=math,
        _PlsNetworkError=OSError,
        _PLS_PUBLIC_KEY=PUBLIC_KEY,
        _PLS_GRACE_SECONDS=43200,
        _PLS_KEY_FP=None,
        _PLS_VALID_UNTIL=0,
        _PLS_MODE="none",
        _PLS_LAST_WALL=0,
    )
