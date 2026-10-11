"""Flask server that receives Zerodha login redirects and exchanges the request_token.

Run separately via supervisord (always-on, port 8080).
The callback URL to register in Zerodha's Kite Connect app settings:
    http://YOUR_VPS_IP:8080/callback

After login, Zerodha redirects to:
    http://YOUR_VPS_IP:8080/callback?request_token=XXXX&action=login&status=success
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, jsonify, request

from broker.auth_manager import AuthManager
from config.config import config
from utils.logger import setup_logging

setup_logging()
logger = logging.getLogger("trading.auth_server")

app = Flask(__name__)
_auth = AuthManager()


@app.route("/callback")
def callback():
    """Zerodha redirects here after successful login."""
    status = request.args.get("status")
    request_token = request.args.get("request_token")

    if status != "success" or not request_token:
        logger.error("Login callback failed: status=%s, token=%s", status, request_token)
        return jsonify({"error": "Login failed", "status": status}), 400

    access_token = _auth.exchange_request_token(request_token)
    if not access_token:
        logger.error("Token exchange failed for request_token=%s", request_token[:10])
        return jsonify({"error": "Token exchange failed"}), 500

    logger.info("Login successful — access token stored")
    return (
        "<html><body style='font-family:sans-serif;text-align:center;padding:50px'>"
        "<h2>✅ Login Successful</h2>"
        "<p>Access token stored. The trading bot is now authenticated for today.</p>"
        "<p>You can close this window.</p>"
        "</body></html>"
    )


@app.route("/status")
def status():
    """Health check endpoint — returns current auth status."""
    has_token = _auth.is_authenticated()
    return jsonify(
        {
            "authenticated": has_token,
            "paper_mode": config.PAPER_TRADING_MODE,
        }
    )


@app.route("/logout", methods=["POST"])
def logout():
    """Invalidate the current session (operator action only).

    Port 8080 is internet-facing (Zerodha OAuth redirect), so this
    state-changing endpoint requires the operator secret. If no secret is
    configured the endpoint is disabled outright.
    """
    secret = config.AUTH_OPERATOR_SECRET
    if not secret or request.headers.get("X-Operator-Secret") != secret:
        logger.warning("Rejected /logout request from %s", request.remote_addr)
        return jsonify({"error": "Forbidden"}), 403
    _auth.invalidate()
    return jsonify({"message": "Session invalidated"})


if __name__ == "__main__":
    logger.info(
        "Auth server starting on %s:%d",
        config.AUTH_CALLBACK_HOST,
        config.AUTH_CALLBACK_PORT,
    )
    app.run(
        host=config.AUTH_CALLBACK_HOST,
        port=config.AUTH_CALLBACK_PORT,
        debug=False,
        use_reloader=False,
    )
