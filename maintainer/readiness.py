"""Bounded readiness helpers shared by validation paths."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def wait_for_nonempty(
    getter: Callable[[], T],
    *,
    attempts: int = 3,
    delay_seconds: float = 5,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Retry a transiently empty desktop observation, then fail closed."""
    for attempt in range(attempts):
        value = getter()
        if value:
            return value
        if attempt + 1 < attempts:
            sleep(delay_seconds)
    raise RuntimeError("OSWorld returned an empty accessibility tree")
