"""Local authentication lifecycle checks (no Binance network access).

Expiry metadata can establish that a token SHOULD NOT be used, but cannot
establish that Binance still accepts the session. Only normal user login
can restore a session that requires interactive verification.
"""
import argparse
import hmac
import time
from datetime import datetime, timezone

from secure_storage import load_token

WARN_BEFORE_SECONDS = 24 * 60 * 60
MIN_TRADE_REMAINING_SECONDS = 15 * 60


class AuthenticationRequired(RuntimeError):
    """Credentials are missing, stale, unknown, or different from local state."""


def _expiry_from_token(token):
    raw = token.get("expirationTimestamp")
    if isinstance(raw, bool):
        return None
    try:
        expiry = int(raw)
    except (ValueError, TypeError, OverflowError):
        return None
    if expiry <= 0 or expiry > 253402300799:
        return None
    return expiry


def get_session_status(*, now=None, warn_before_seconds=WARN_BEFORE_SECONDS):
    """Return only non-secret metadata. Never print or return token values."""
    if now is None:
        now = time.time()
    try:
        token = load_token()
    except FileNotFoundError:
        return {"state": "missing", "expiry_utc": None, "remaining_seconds": None}

    expiry = _expiry_from_token(token)
    if expiry is None:
        return {"state": "unknown_expiry", "expiry_utc": None, "remaining_seconds": None}

    remaining = int(expiry - now)
    if remaining <= 0:
        state = "expired"
    elif remaining <= warn_before_seconds:
        state = "expiring"
    else:
        state = "locally_valid"
    return {
        "state": state,
        "expiry_utc": datetime.fromtimestamp(expiry, tz=timezone.utc).isoformat(),
        "remaining_seconds": remaining,
    }


def require_fresh_session(csrftoken, p20t, *, now=None, min_remaining_seconds=MIN_TRADE_REMAINING_SECONDS):
    """Fail closed before order submission when locally stored auth cannot be trusted.

    This validates only local metadata, NOT exchange-side authentication.
    """
    if now is None:
        now = time.time()
    if not isinstance(csrftoken, str) or not isinstance(p20t, str):
        raise AuthenticationRequired("Credentials must be read from the secure local state")
    try:
        local = load_token()
    except (FileNotFoundError, ValueError, PermissionError) as exc:
        raise AuthenticationRequired("No valid private session file; authenticate interactively") from None

    if not (
        hmac.compare_digest(csrftoken, local["csrftoken"])
        and hmac.compare_digest(p20t, local["p20t"])
    ):
        raise AuthenticationRequired("Provided session does not match locally stored credentials")

    expiry = _expiry_from_token(local)
    if expiry is None:
        raise AuthenticationRequired("Session expiry is unknown; re-authenticate before trading")
    if expiry - now <= min_remaining_seconds:
        raise AuthenticationRequired("Session is expired or too close to expiry; re-authenticate")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect local Binance session without revealing tokens")
    parser.add_argument("command", nargs="?", choices=["status"], default="status")
    parser.add_argument("--warn-hours", type=float, default=24, help="Warning threshold; default 24h")
    args = parser.parse_args(argv)
    if not (0 <= args.warn_hours <= 24 * 30):
        parser.error("--warn-hours must be between 0 and 720")
    status = get_session_status(warn_before_seconds=args.warn_hours * 3600)
    print("Authentication state (local metadata only, not server verified):", status["state"])
    if status["expiry_utc"]:
        print("Stored expiry (UTC):", status["expiry_utc"])
        print("Seconds remaining:", status["remaining_seconds"])
    if status["state"] in ("missing", "expired", "unknown_expiry"):
        print("Action: stop trading and re-authenticate using Binance App QR confirmation")
        return 2
    if status["state"] == "expiring":
        print("Action: schedule interactive renewal; no guaranteed automatic refresh")
        return 1
    print("Action: locally unexpired; exchange-side session validity remains unverified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
