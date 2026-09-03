"""Small allowlisting helpers for shareable benchmark receipts."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def public_error(error: BaseException) -> dict[str, str]:
    """Describe an exception without copying its potentially secret-bearing text."""
    return {"error_type": type(error).__name__}


def public_transport(value: str | None) -> str | None:
    """Keep endpoint routing metadata while dropping credentials and URL secrets."""
    if not value:
        return None
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.hostname:
        return None
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return None
    return urlunsplit((parsed.scheme, host + port, parsed.path, "", ""))
