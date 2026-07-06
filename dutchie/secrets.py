"""At-rest secret decryption (Fernet) — vendored from monorepo apps/tenants/secrets.py.

Encrypted values are "enc:v1:<ciphertext>". Untagged (plaintext) values pass through
unchanged — that's the legacy/dev path. Key derives from BUDTENDER_FIELD_KEY (PBKDF2).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

ENC_TAG = "enc:v1:"
_PBKDF2_ITERATIONS = 600_000
_SALT = b"budtender-pos-secret-encryption-v1"
_fernet = None


def _key_material() -> str:
    # Standalone: read the key from env. Empty key => derive from a dev default so
    # plaintext-only setups still work (only tagged ciphertext needs the real key).
    return os.environ.get("BUDTENDER_FIELD_KEY") or "dev-insecure-key-set-BUDTENDER_FIELD_KEY"


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        derived = hashlib.pbkdf2_hmac(
            "sha256", _key_material().encode(), _SALT, iterations=_PBKDF2_ITERATIONS
        )
        _fernet = Fernet(base64.urlsafe_b64encode(derived[:32]))
    return _fernet


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(ENC_TAG)


def encrypt_secret(plaintext: str) -> str:
    if plaintext is None or is_encrypted(plaintext):
        return plaintext
    if not isinstance(plaintext, str):
        plaintext = str(plaintext)
    return ENC_TAG + _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(value: str):
    """Decrypt a tagged value; pass plaintext through. Never raises (bad token -> None)."""
    if not is_encrypted(value):
        return value
    try:
        return _get_fernet().decrypt(value[len(ENC_TAG):].encode()).decode()
    except InvalidToken:
        logger.error("secret decrypt failed: invalid token (key changed?)")
        return None
