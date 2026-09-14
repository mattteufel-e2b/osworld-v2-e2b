from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))
from readiness import wait_for_nonempty  # noqa: E402


def test_wait_for_nonempty_retries_transient_empty_values():
    values = iter((None, "", "ready"))
    sleeps: list[float] = []

    assert (
        wait_for_nonempty(
            lambda: next(values), attempts=3, delay_seconds=0.25, sleep=sleeps.append
        )
        == "ready"
    )
    assert sleeps == [0.25, 0.25]


def test_wait_for_nonempty_fails_after_the_bounded_attempts():
    with pytest.raises(RuntimeError, match="empty accessibility tree"):
        wait_for_nonempty(
            lambda: "", attempts=3, delay_seconds=0, sleep=lambda _seconds: None
        )
