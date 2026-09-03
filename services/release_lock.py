#!/usr/bin/env python3
"""Validate immutable source and service-image pins in the release lock."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

GITLAB_REPOSITORY = "Task-Web/gitlab"
SERVICE_IMAGE_REPOSITORIES = {
    "fanout": "nginx",
    "gitlab": "gitlab/gitlab-ce",
    "gitlab_init": "docker",
    "gitlab_runner": "gitlab/gitlab-runner",
}
REQUIRED_SERVICE_IMAGES = frozenset(SERVICE_IMAGE_REPOSITORIES)
COMMIT_RE = re.compile(r"[0-9a-f]{40}")


def validate_release_lock(path: Path) -> dict:
    """Return a validated lock or raise ValueError before runtime allocation."""
    try:
        lock = json.loads(path.read_text())
        gitlab = lock["gitlab_code"]
        repository = gitlab["repository"]
        commit = gitlab["commit"]
        images = lock["service_images"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"release lock invalid: {exc}") from exc

    if repository != GITLAB_REPOSITORY:
        raise ValueError(
            "release lock invalid: gitlab_code.repository must be "
            f"{GITLAB_REPOSITORY!r}"
        )
    if not isinstance(commit, str) or COMMIT_RE.fullmatch(commit) is None:
        raise ValueError(
            "release lock invalid: gitlab_code.commit must be a 40-character "
            "lowercase hexadecimal commit"
        )
    if not isinstance(images, dict):
        raise ValueError("release lock invalid: service_images must be an object")

    missing = REQUIRED_SERVICE_IMAGES - images.keys()
    if missing:
        raise ValueError(
            "release lock invalid: missing service image pins: "
            + ", ".join(sorted(missing))
        )
    for name in sorted(REQUIRED_SERVICE_IMAGES):
        image = images[name]
        repository = SERVICE_IMAGE_REPOSITORIES[name]
        expected = re.compile(rf"{re.escape(repository)}@sha256:[0-9a-f]{{64}}")
        if not isinstance(image, str) or expected.fullmatch(image) is None:
            raise ValueError(
                f"release lock invalid: service image {name!r} must be "
                f"{repository!r} at an immutable lowercase sha256 digest"
            )
    return lock


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} LOCKFILE", file=sys.stderr)
        return 2
    try:
        validate_release_lock(Path(argv[1]))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
