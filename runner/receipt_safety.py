"""Small allowlisting helpers for shareable benchmark receipts."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
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


def atomic_write_json(path: Path, payload: dict) -> None:
    """Publish a private (0600) receipt atomically: a reader sees the old file
    or the complete new one, never a truncation — a SIGTERM/SIGKILL landing
    mid-write must not cost the receipt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staged_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as staged:
            staged_path = Path(staged.name)
            os.chmod(staged_path, 0o600)
            json.dump(payload, staged, indent=2, sort_keys=True, allow_nan=False)
            staged.write("\n")
            staged.flush()
            os.fsync(staged.fileno())
        os.replace(staged_path, path)
        staged_path = None
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)
