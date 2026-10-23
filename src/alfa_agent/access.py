"""Shared team login for the hosted hackathon prototype; not bank SSO."""

import hashlib
import hmac
import secrets
import time

COOKIE = "alfa_team_session"
SESSION_SECONDS = 12 * 60 * 60


def issue_session(password):
    payload = f"{int(time.time()) + SESSION_SECONDS}.{secrets.token_hex(16)}"
    signature = hmac.new(password.encode(), ("alfa-session:" + payload).encode(), hashlib.sha256).hexdigest()
    return payload + "." + signature


def valid_session(cookie, password):
    try:
        expiry, nonce, signature = cookie.split(".")
        deadline = int(expiry)
        now = int(time.time())
        if not now < deadline <= now + SESSION_SECONDS or len(nonce) != 32:
            return False
        expected = hmac.new(
            password.encode(), ("alfa-session:" + expiry + "." + nonce).encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(signature.encode(), expected.encode())
    except (ValueError, TypeError, AttributeError):
        return False
