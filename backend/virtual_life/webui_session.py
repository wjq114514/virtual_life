"""Server-signed WebUI session cookie.

The browser only ever holds this cookie, never the administrative token. The
value is an HMAC over an expiry timestamp, so nothing has to be stored:
rotating the API token invalidates every session, and there is no revocation
table to keep in step with it.
"""

from __future__ import annotations

import hashlib
import hmac
import time

COOKIE_NAME = "vl_webui_session"
SESSION_SCHEME = "v1"
SECONDS_PER_DAY = 86_400


def sign_session_cookie(secret: str, expires_at: int) -> str:
    """Sign an expiry timestamp; the API token never appears in the cookie."""
    payload = f"{SESSION_SCHEME}.{expires_at}".encode("ascii")
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"{payload.decode('ascii')}.{digest}"


def verify_session_cookie(secret: str, value: str, *, at: float | None = None) -> bool:
    """Check scheme, expiry and digest without distinguishing which failed."""
    scheme, separator, rest = value.partition(".")
    expiry_text, separator, digest = rest.partition(".")
    if not separator or not digest:
        return False
    if scheme != SESSION_SCHEME or not expiry_text.isdigit():
        return False
    if int(expiry_text) < (at if at is not None else time.time()):
        return False
    payload = f"{SESSION_SCHEME}.{expiry_text}".encode("ascii")
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, expected)
