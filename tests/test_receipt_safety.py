from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))
from receipt_safety import public_error, public_transport  # noqa: E402


def test_public_error_never_serializes_exception_text():
    secret = "glpat-super-secret"
    payload = public_error(RuntimeError(f"request failed with token {secret}"))

    assert payload == {"error_type": "RuntimeError"}
    assert secret not in json.dumps(payload)


def test_public_transport_strips_credentials_query_and_fragment():
    secret = "api-key-secret"

    value = public_transport(
        f"https://user:{secret}@api.example.test:8443/v1/chat?key={secret}#debug"
    )

    assert value == "https://api.example.test:8443/v1/chat"
    assert secret not in value
