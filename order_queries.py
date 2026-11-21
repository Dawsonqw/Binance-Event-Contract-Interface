"""Local audit-log query adapter and unsupported remote query placeholders.

This module reads only LOCAL submission records. Separately, the
binance_event_queries.BinanceEventQueryProvider implements the two verified
read-only Binance order LIST endpoints. Arbitrary order-ID lookup, live payout
quotes and independent settlement lookup are NOT verified.
"""
import hashlib
import json
import os
import re
import stat
from pathlib import Path

from secure_storage import state_dir

_INTENT = re.compile(r"[A-Za-z0-9_.:-]{1,100}\Z")
_FIELDS = ("orderAmount", "timeIncrements", "symbolName", "payoutRatio", "direction")
_MAX_RECORD_BYTES = 16384


class RemoteQueryUnavailable(NotImplementedError):
    """No verified Binance event-contract query integration is configured."""


class LocalRecordError(RuntimeError):
    """Local order audit record cannot be read or verified."""


def _validate_id(intent_id):
    if not isinstance(intent_id, str) or not _INTENT.fullmatch(intent_id):
        raise ValueError("intent_id must contain 1-100 safe ASCII characters")
    return intent_id


def _record_directory():
    directory = state_dir() / "intents"
    if directory.is_symlink():
        raise LocalRecordError("Unsafe local order directory")
    return directory


def _read_private_json(path):
    """Bounded read of owner-only regular files; never follow a symlink."""
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        raise LocalRecordError("Cannot read local order record") from None
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or (stat.S_IMODE(metadata.st_mode) & 0o077):
            raise LocalRecordError("Unsafe local order record permissions")
        if metadata.st_size > _MAX_RECORD_BYTES:
            raise LocalRecordError("Local order record is too large")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            data = json.load(stream)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise LocalRecordError("Local order record is invalid") from None
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(data, dict):
        raise LocalRecordError("Local order record is invalid")
    return data


def get_local_order(intent_id):
    """Get locally recorded submission state; NEVER infer exchange settlement."""
    _validate_id(intent_id)
    directory = _record_directory()
    hashed = hashlib.sha256(intent_id.encode("utf-8")).hexdigest()
    submission = _read_private_json(directory / f"{hashed}.json")
    if submission is None:
        return None
    if submission.get("intent_id") != intent_id:
        raise LocalRecordError("Local order identifier mismatch")
    raw_order = submission.get("order")
    if not isinstance(raw_order, dict):
        raise LocalRecordError("Local order payload invalid")
    confirmed = _read_private_json(directory / f"{hashed}.confirmed")
    if confirmed is not None:
        order_id = confirmed.get("order_id")
        if not isinstance(order_id, str) or not order_id.strip():
            raise LocalRecordError("Local API acknowledgement is malformed")
        status = "api_acknowledged_not_settlement_verified"
    else:
        order_id = None
        status = "submission_attempted_outcome_unknown"

    return {
        "intent_id": intent_id,
        "status": status,
        "source": "local_audit_only",
        "exchange_verified": False,
        "submitted_at_utc": submission.get("submitted_at_utc") if isinstance(submission.get("submitted_at_utc"), str) else None,
        "order_id": order_id,
        "order": {
            field: raw_order[field] for field in _FIELDS
            if field in raw_order and isinstance(raw_order[field], str)
        },
    }


def list_local_orders(limit=50):
    if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 100):
        raise ValueError("limit must be an integer between 1 and 100")
    directory = _record_directory()
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise LocalRecordError("Local order directory is invalid")

    # The local ledger is intentionally simple. A future production gateway
    # should migrate these append-only records to a transactionally indexed DB.
    records = []
    for path in directory.glob("*.json"):
        if path.is_symlink():
            raise LocalRecordError("Unsafe symlink in local order directory")
        submission = _read_private_json(path)
        if submission is None:
            continue
        intent_id = submission.get("intent_id")
        try:
            _validate_id(intent_id)
        except ValueError:
            raise LocalRecordError("Invalid intent ID in local ledger") from None
        expected_name = hashlib.sha256(intent_id.encode("utf-8")).hexdigest() + ".json"
        if path.name != expected_name:
            raise LocalRecordError("Local order hash mismatch")
        records.append(get_local_order(intent_id))
    records.sort(key=lambda row: row["submitted_at_utc"] or "", reverse=True)
    return records[:limit]


def query_exchange_order(order_id):
    """Reserved extension point: requires a verified official/authorized adapter."""
    raise RemoteQueryUnavailable(
        "Event contract exchange order query is not verified; only local audit records are supported"
    )


def query_market_quote(symbol, time_increments):
    """Never invent a quote or reuse a stale, hardcoded payout ratio."""
    raise RemoteQueryUnavailable(
        "Event contract live payout quote query is not verified"
    )


def query_settlement(order_id):
    raise RemoteQueryUnavailable(
        "Event contract settlement query is not verified"
    )
