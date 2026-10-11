"""Encrypt and decrypt the Zerodha access token stored on disk.

Uses Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256).
The encryption key is stored only in the environment variable TOKEN_ENCRYPTION_KEY,
never on disk alongside the encrypted token.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from config.config import config

logger = logging.getLogger("data")

_TOKEN_FILE = Path("data/.access_token.enc")


def _get_fernet() -> Fernet:
    """Return the Fernet instance using the key from environment."""
    key = config.TOKEN_ENCRYPTION_KEY
    if not key:
        raise ValueError(
            "TOKEN_ENCRYPTION_KEY environment variable is not set. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as exc:
        raise ValueError(f"Invalid TOKEN_ENCRYPTION_KEY: {exc}") from exc


def save_access_token(access_token: str) -> None:
    """Encrypt and persist the access token to disk.

    Args:
        access_token: The raw Zerodha Kite access token string.

    Raises:
        ValueError: If the encryption key is not configured.
        OSError:    If the token file cannot be written.
    """
    fernet = _get_fernet()
    payload = json.dumps(
        {
            "token": access_token,
            "saved_at": datetime.now(tz=timezone.utc).isoformat(),
        }
    ).encode()
    encrypted = fernet.encrypt(payload)
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_bytes(encrypted)
    logger.info("Access token saved (encrypted) to %s", _TOKEN_FILE)


def load_access_token() -> Optional[str]:
    """Decrypt and return the stored access token.

    Returns:
        The access token string, or None if the file does not exist
        or the token has expired (Zerodha tokens expire at midnight IST).

    Raises:
        ValueError: If the encryption key is not configured or the token is corrupted.
    """
    if not _TOKEN_FILE.exists():
        logger.debug("No stored access token found at %s", _TOKEN_FILE)
        return None

    fernet = _get_fernet()
    try:
        encrypted = _TOKEN_FILE.read_bytes()
        payload = json.loads(fernet.decrypt(encrypted).decode())
    except InvalidToken as exc:
        logger.error("Access token decryption failed — key mismatch or corrupted file: %s", exc)
        return None
    except json.JSONDecodeError as exc:
        logger.error("Decrypted token payload is not valid JSON: %s", exc)
        return None

    saved_at = datetime.fromisoformat(payload["saved_at"])
    age_hours = (datetime.now(tz=timezone.utc) - saved_at).total_seconds() / 3600

    # Zerodha access tokens are valid until midnight IST (~18.5 hours max from 8 AM)
    if age_hours > 20:
        logger.warning("Stored access token is %0.1f hours old — likely expired", age_hours)
        return None

    return payload["token"]


def delete_access_token() -> None:
    """Remove the stored access token file."""
    if _TOKEN_FILE.exists():
        _TOKEN_FILE.unlink()
        logger.info("Access token file deleted")


def generate_new_key() -> str:
    """Generate a new Fernet encryption key. Run once during setup."""
    return Fernet.generate_key().decode()
