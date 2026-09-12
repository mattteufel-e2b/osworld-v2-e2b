#!/usr/bin/env python3
"""Validate immutable source and service-image pins in the release lock."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

GITLAB_REPOSITORY = "Task-Web/gitlab"
WEBSITES_REPOSITORY = "Task-Web/OSWorld-web"
OSWORLD_REPOSITORY = "xlang-ai/OSWorld-V2"
SERVER_REPOSITORY = "xlang-ai/osworld-server"
SUITE = "osworld-v2"
RELEASE_RE = re.compile(r"osworld-v2-[0-9]{4}\.[0-9]{2}\.[0-9]{2}")
SERVICE_IMAGE_REPOSITORIES = {
    "fanout": "nginx",
    "gitlab": "gitlab/gitlab-ce",
    "gitlab_init": "docker",
    "gitlab_runner": "gitlab/gitlab-runner",
}
REQUIRED_SERVICE_IMAGES = frozenset(SERVICE_IMAGE_REPOSITORIES)
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")


def validate_release_lock(path: Path) -> dict:
    """Return a validated lock or raise ValueError before runtime allocation."""
    try:
        lock = json.loads(path.read_text())
        suite = lock["suite"]
        release = lock["release"]
        code = lock["code"]
        server = lock["server_code"]
        gitlab = lock["gitlab_code"]
        websites = lock["websites_code"]
        tasks_data = lock["tasks_data"]
        assets_data = lock["assets_data"]
        images = lock["service_images"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"release lock invalid: {exc}") from exc

    if suite != SUITE:
        raise ValueError(f"release lock invalid: suite must be {SUITE!r}")
    if not isinstance(release, str) or RELEASE_RE.fullmatch(release) is None:
        raise ValueError("release lock invalid: release must be osworld-v2-YYYY.MM.DD")

    for name, source, expected_repository in (
        ("code", code, OSWORLD_REPOSITORY),
        ("server_code", server, SERVER_REPOSITORY),
        ("gitlab_code", gitlab, GITLAB_REPOSITORY),
        ("websites_code", websites, WEBSITES_REPOSITORY),
    ):
        if not isinstance(source, dict):
            raise ValueError(f"release lock invalid: {name} must be an object")
        repository = source.get("repository")
        commit = source.get("commit")
        if repository != expected_repository:
            raise ValueError(
                f"release lock invalid: {name}.repository must be "
                f"{expected_repository!r}"
            )
        if not isinstance(commit, str) or COMMIT_RE.fullmatch(commit) is None:
            raise ValueError(
                f"release lock invalid: {name}.commit must be a 40-character "
                "lowercase hexadecimal commit"
            )

    for name, source, expected_repository in (
        ("tasks_data", tasks_data, "xlangai/osworld_v2_tasks"),
        ("assets_data", assets_data, "xlangai/osworld_v2_assets_gated"),
    ):
        if not isinstance(source, dict):
            raise ValueError(f"release lock invalid: {name} must be an object")
        if source.get("repository") != expected_repository:
            raise ValueError(
                f"release lock invalid: {name}.repository must be {expected_repository!r}"
            )
        revision = source.get("revision")
        if not isinstance(revision, str) or COMMIT_RE.fullmatch(revision) is None:
            raise ValueError(
                f"release lock invalid: {name}.revision must be a 40-character "
                "lowercase hexadecimal commit"
            )
    if tasks_data.get("hash_manifest") != "examples/osworld-v2/task-hashes.json":
        raise ValueError(
            "release lock invalid: tasks_data.hash_manifest must be "
            "'examples/osworld-v2/task-hashes.json'"
        )
    manifest_digest = tasks_data.get("manifest_sha256")
    if (
        not isinstance(manifest_digest, str)
        or DIGEST_RE.fullmatch(manifest_digest) is None
    ):
        raise ValueError(
            "release lock invalid: tasks_data.manifest_sha256 must be a 64-character digest"
        )
    if tasks_data.get("task_count") != 108:
        raise ValueError("release lock invalid: tasks_data.task_count must be 108")
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
