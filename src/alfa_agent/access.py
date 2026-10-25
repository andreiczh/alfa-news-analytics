"""Shared team login for the hosted hackathon prototype; not bank SSO."""

import hashlib
import hmac
import secrets
import time
import base64
import json
import os

from .identity import Principal

COOKIE = "alfa_team_session"
SESSION_SECONDS = 12 * 60 * 60


def signing_key(settings, password):
    """A shared login password must never let users forge another user's cookie."""
    secret = os.environ.get("ALFA_SESSION_SECRET", "").strip()
    if not secret:
        if settings.public_deployment:
            raise ValueError("public_deployment_requires_session_secret")
        settings.state_path.mkdir(parents=True, exist_ok=True)
        path = settings.state_path / "session.key"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            secret = path.read_text().strip()
        else:
            secret = secrets.token_urlsafe(48)
            with os.fdopen(fd, "w") as file:
                file.write(secret)
    if len(secret) < 32:
        raise ValueError("session_secret_too_short")
    return hmac.new(secret.encode(), b"alfa-signing:" + password.encode(), hashlib.sha256).hexdigest()


def issue_session(password, principal=None, session_id=None):
    nonce = (
        secrets.token_hex(16)
        if principal is None
        else base64.urlsafe_b64encode(
            json.dumps(
                {**principal.public(), **({"_sid": session_id} if session_id else {})}, ensure_ascii=False
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    payload = f"{int(time.time()) + SESSION_SECONDS}.{nonce}"
    signature = hmac.new(password.encode(), ("alfa-session:" + payload).encode(), hashlib.sha256).hexdigest()
    return payload + "." + signature


def valid_session(cookie, password):
    try:
        expiry, nonce, signature = cookie.split(".")
        deadline = int(expiry)
        now = int(time.time())
        if not now < deadline <= now + SESSION_SECONDS or not 32 <= len(nonce) <= 3000:
            return False
        expected = hmac.new(
            password.encode(), ("alfa-session:" + expiry + "." + nonce).encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(signature.encode(), expected.encode())
    except (ValueError, TypeError, AttributeError):
        return False


def session_principal(cookie, password):
    if not valid_session(cookie, password):
        return None
    try:
        payload = cookie.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        data.pop("_sid", None)
        principal = Principal(**data)
        if not principal.user_id.startswith("user_") or not principal.company_id.startswith("company_"):
            return None
        return principal
    except (ValueError, TypeError, KeyError):
        return None


def session_nonce(cookie, password):
    if not valid_session(cookie, password):
        return None
    try:
        payload = cookie.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        sid = data.get("_sid")
        return sid if isinstance(sid, str) and 32 <= len(sid) <= 100 else None
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
