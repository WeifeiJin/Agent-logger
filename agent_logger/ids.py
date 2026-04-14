from __future__ import annotations

from datetime import datetime, timezone
import secrets
import threading


_counter_lock = threading.Lock()
_counter = 0


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def utc_timestamp_from_epoch(epoch_seconds: float | int) -> str:
    return datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc).isoformat(timespec="milliseconds")


def _next_counter() -> int:
    global _counter
    with _counter_lock:
        _counter += 1
        return _counter


def make_session_id(prefix: str = "sess") -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}_{secrets.token_hex(3)}"


def make_trace_id(prefix: str = "trace") -> str:
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}"


def make_event_id(prefix: str = "evt") -> str:
    return f"{prefix}_{_next_counter():06d}_{secrets.token_hex(2)}"
