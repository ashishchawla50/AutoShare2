"""Zerodha authentication manager — semi-automated daily token refresh.

Recommended flow (ToS-compliant):
  1. 8:00 AM: Generate login URL, send via Telegram
  2. Operator opens URL, logs in manually (30 seconds)
  3. Zerodha redirects to Flask callback with request_token
  4. auth_server.py exchanges request_token for access_token
  5. access_token encrypted and stored on disk
  6. Bot loads token and begins operations

TOTP automation code is included but clearly flagged.
Use at your own risk — may violate Zerodha's ToS.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime
from typing import Optional

from config.config import config
from utils.encryption import load_access_token, save_access_token, delete_access_token

logger = logging.getLogger("trading.auth")


class AuthManager:
    """Manages Zerodha session lifecycle.

    In paper trading mode, authentication is optional — the bot
    will run without a live Kite connection (using cached/simulated data).
    """

    def __init__(self) -> None:
        self._access_token: Optional[str] = None

    def get_login_url(self) -> str:
        """Return the Zerodha login URL for today's session.

        The trader opens this URL, logs in, and gets redirected to our callback.
        """
        try:
            from kiteconnect import KiteConnect  # type: ignore[import]
            kite = KiteConnect(api_key=config.ZERODHA_API_KEY)
            return kite.login_url()
        except ImportError:
            # kiteconnect not installed — return a placeholder URL for paper mode
            return f"https://kite.zerodha.com/connect/login?api_key={config.ZERODHA_API_KEY}&v=3"
        except Exception as exc:
            logger.error("Failed to generate login URL: %s", exc)
            return ""

    def exchange_request_token(self, request_token: str) -> Optional[str]:
        """Exchange a request_token (from redirect) for an access_token.

        Called automatically by auth_server.py when the redirect arrives.

        Args:
            request_token: The token from Zerodha's login redirect.

        Returns:
            The access_token string, or None on failure.
        """
        try:
            from kiteconnect import KiteConnect  # type: ignore[import]
            kite = KiteConnect(api_key=config.ZERODHA_API_KEY)
            session_data = kite.generate_session(
                request_token=request_token,
                api_secret=config.ZERODHA_API_SECRET,
            )
            access_token = session_data["access_token"]
            save_access_token(access_token)
            self._access_token = access_token
            logger.info("Access token exchanged and saved successfully")
            return access_token
        except Exception as exc:
            logger.error("Token exchange failed: %s", exc)
            return None

    def load_token(self) -> Optional[str]:
        """Load today's access token from encrypted storage.

        Returns:
            The access token string, or None if not available or expired.
        """
        token = load_access_token()
        if token:
            self._access_token = token
            logger.debug("Access token loaded from disk")
        return token

    def get_access_token(self) -> Optional[str]:
        """Return the current access token (load from disk if not in memory)."""
        if self._access_token:
            return self._access_token
        return self.load_token()

    def is_authenticated(self) -> bool:
        """Return True if a valid access token is available."""
        return self.get_access_token() is not None

    def invalidate(self) -> None:
        """Clear the current session (called at market close or on error)."""
        self._access_token = None
        delete_access_token()
        logger.info("Access token invalidated")

    # ── TOTP automation (use at your own risk) ────────────────────────────────

    def login_with_totp(self) -> Optional[str]:
        """Fully automated headless login using TOTP.

        WARNING: Zerodha's Terms of Service may prohibit fully automated login.
        This method automates the 2FA step. Use only if you accept the ToS risk.
        Zerodha may suspend accounts that abuse automated login.

        Returns:
            Access token string, or None if login fails.
        """
        if not config.ZERODHA_TOTP_SECRET:
            logger.error("ZERODHA_TOTP_SECRET not set — cannot use TOTP automation")
            return None

        try:
            import pyotp  # type: ignore[import]
            import requests

            totp_code = pyotp.TOTP(config.ZERODHA_TOTP_SECRET).now()

            session = requests.Session()
            session.headers.update({"User-Agent": "Mozilla/5.0", "Content-Type": "application/x-www-form-urlencoded"})

            # Step 1: Password login
            resp1 = session.post(
                "https://kite.zerodha.com/api/login",
                data={
                    "user_id": config.ZERODHA_USER_ID,
                    "password": config.ZERODHA_PASSWORD,
                },
                timeout=15,
            )
            resp1.raise_for_status()
            data1 = resp1.json()
            request_id = data1["data"]["request_id"]

            # Step 2: TOTP 2FA
            resp2 = session.post(
                "https://kite.zerodha.com/api/twofa",
                data={
                    "user_id": config.ZERODHA_USER_ID,
                    "request_id": request_id,
                    "twofa_value": totp_code,
                    "twofa_type": "totp",
                },
                timeout=15,
            )
            resp2.raise_for_status()

            # Step 3: Get request_token from Kite Connect redirect
            # Zerodha does a two-step redirect:
            #   login_url → kite.zerodha.com/connect/finish?sess_id=... → callback?request_token=...
            from kiteconnect import KiteConnect  # type: ignore[import]
            kite = KiteConnect(api_key=config.ZERODHA_API_KEY)
            login_url = kite.login_url()

            resp3 = session.get(login_url, timeout=15, allow_redirects=False)
            location = resp3.headers.get("Location", "")

            # Follow one more redirect if request_token not in first location
            if "request_token=" not in location and location:
                resp4 = session.get(location, timeout=15, allow_redirects=False)
                location = resp4.headers.get("Location", "")

            if "request_token=" not in location:
                logger.error("TOTP login: no request_token in redirect — %s", location)
                return None

            request_token = location.split("request_token=")[1].split("&")[0]
            return self.exchange_request_token(request_token)

        except Exception as exc:
            logger.error("TOTP automated login failed: %s", exc)
            return None
