"""Conservative, opt-in Binance event-contract order submission.

IMPORTANT: the endpoint is undocumented; no server-side idempotency or order
query API is known. Local intent records prevent accidental repeats on the
same state volume, but cannot establish an exchange-side order state.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests

from secure_storage import state_dir
from session_lifecycle import require_fresh_session

ORDER_URL = "https://www.binance.com/bapi/futures/v2/private/future/event-contract/place-order"
LIVE_OPT_IN = "I_ACCEPT_REAL_MONEY_RISK"


class OrderNotConfirmed(RuntimeError):
    """Exchange-side outcome is unknown: DO NOT automatically retry."""


class DuplicateIntent(RuntimeError):
    """This intent has already been attempted locally."""


def _positive_decimal(value, field):
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"Invalid {field}") from None
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{field} must be a finite positive number")
    return parsed


def _validate(order_amount, time_increments, symbol, payout_ratio, direction, intent_id):
    if not isinstance(intent_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", intent_id):
        raise ValueError("A stable, unique intent_id is required")
    if direction not in ("LONG", "SHORT"):
        raise ValueError("direction must be LONG or SHORT")
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{5,25}", symbol):
        raise ValueError("Invalid symbolName")
    if not isinstance(time_increments, str) or not re.fullmatch(r"[A-Z_]{3,40}", time_increments):
        raise ValueError("Invalid timeIncrements")
    amount = _positive_decimal(order_amount, "orderAmount")
    _positive_decimal(payout_ratio, "payoutRatio")
    maximum = _positive_decimal(os.environ.get("BINANCE_EVENT_MAX_ORDER_USDT", "5"), "maximum order amount")
    if amount > maximum:
        raise ValueError(f"orderAmount exceeds configured cap ({maximum} USDT)")
    return {
        "orderAmount": str(amount),
        "timeIncrements": time_increments,
        "symbolName": symbol,
        "payoutRatio": str(payout_ratio),
        "direction": direction,
    }


def _record_intent(intent_id, fields):
    """Exclusive-create an irreversible local marker BEFORE any network I/O."""
    directory = state_dir() / "intents"
    if directory.is_symlink():
        raise RuntimeError("Refusing a symlinked intents directory")
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    fingerprint = hashlib.sha256(intent_id.encode("utf-8")).hexdigest()
    path = directory / f"{fingerprint}.json"
    record = {
        "intent_id": intent_id,
        "submitted_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "submission_attempted_outcome_unknown",
        "order": fields,
    }
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise DuplicateIntent("Intent already attempted; reconcile exchange orders before any new attempt") from None
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        # Ensure the exclusive marker itself survives a crash before network I/O.
        directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        # Preserve the marker even if the write was interrupted: fail closed.
        raise
    return path


def _record_confirmation(path, order_id):
    """Separate confirmation file; original duplicate-protection marker stays."""
    result_path = path.with_suffix(".confirmed")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(result_path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({"order_id": str(order_id), "confirmed_at_utc": datetime.now(timezone.utc).isoformat()}, stream)
        stream.flush()
        os.fsync(stream.fileno())


def _confirmed_order_id(payload):
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    for key in ("orderId", "orderNo", "orderID"):
        value = data.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value)
    return None


def place_order_web(csrftoken, p20t, orderAmount, timeIncrements, symbolName,
                    payoutRatio, direction, *, intent_id):
    """Submit at most once per intent_id. Never retry unknown outcomes.

    Requires explicit live opt-in, non-root execution and a per-order cap.
    A response is considered confirmed only if it has success=true and an
    order identifier; users must independently reconcile in Binance App.
    """
    if os.geteuid() == 0:
        raise PermissionError("Run trading under a dedicated unprivileged Linux user")
    if os.environ.get("BINANCE_EVENT_LIVE_TRADING") != LIVE_OPT_IN:
        raise PermissionError("Live trading disabled; explicit opt-in required")
    if not isinstance(csrftoken, str) or not csrftoken or not isinstance(p20t, str) or not p20t:
        raise ValueError("Missing Binance session credentials")
    # Reject unknown / expiring local credentials BEFORE writing a permanent intent.
    # This check does not prove exchange-side acceptance.
    require_fresh_session(csrftoken, p20t)
    fields = _validate(orderAmount, timeIncrements, symbolName, payoutRatio, direction, intent_id)
    intent_path = _record_intent(intent_id, fields)
    headers = {
        "content-type": "application/json",
        "clienttype": "web",
        "csrftoken": csrftoken,
        "cookie": f"p20t={p20t}",
    }
    try:
        # No automatic retries: a timeout might still have created an order.
        response = requests.post(ORDER_URL, headers=headers, json=fields, timeout=(5, 12), allow_redirects=False)
        response.raise_for_status()
        if not (200 <= response.status_code < 300):
            raise OrderNotConfirmed("Unexpected HTTP redirect or response; reconcile in Binance App")
        payload = response.json()
    except (requests.RequestException, ValueError):
        raise OrderNotConfirmed(
            "Order outcome unknown. Do NOT retry; verify orders in Binance App."
        ) from None

    order_id = _confirmed_order_id(payload)
    if order_id is None:
        raise OrderNotConfirmed(
            "No verified order identifier in response. Do NOT retry; inspect Binance App."
        )
    _record_confirmation(intent_path, order_id)
    return payload
