from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_COMPONENTS = {
    ".cache",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}
FORBIDDEN_EXACT_PATHS = {
    "template/files/server/main.py",
}
FORBIDDEN_PREFIXES = (
    "OSWorld-V2/",
    "out/osworld-v2-raw/",
    "tasks/",
    "template/files/server/src/",
    "template/results/",
)


def _tracked_paths() -> list[str]:
    output = subprocess.check_output(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
    )
    return [path for path in output.decode().split("\0") if path]


def _forbidden_reason(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    name = parts[-1]

    if any(part == ".env" or part.startswith(".env.") for part in parts[:-1]):
        return "dotenv directory"
    if name != ".env.example" and (name == ".env" or name.startswith(".env.")):
        return "dotenv file"
    if path in FORBIDDEN_EXACT_PATHS:
        return "local runtime or generated payload"
    if path.startswith(FORBIDDEN_PREFIXES):
        return "vendored, gated, generated, or raw output tree"
    if any(
        part in FORBIDDEN_COMPONENTS or part.endswith(".egg-info") for part in parts
    ):
        return "cache, dependency, build, or package metadata"
    if name.endswith((".pid", ".pyc", ".pyo")):
        return "runtime or cache file"
    if parts[0] == "services":
        if name.startswith(".runtime") and name.endswith(".json"):
            return "service runtime state"
        if name.endswith(("-token", ".log", ".token")):
            return "service credential or runtime log"
    return None


def test_forbidden_matcher_covers_root_and_nested_dotenv_paths():
    cases = (
        (".env", True),
        (".env.local", True),
        (".env.example", False),
        ("nested/.env", True),
        ("nested/.env.production", True),
        ("nested/.env.example", False),
        ("nested/.env.example.local", True),
        ("nested/.env/secrets", True),
        ("nested/.env.production/secrets", True),
        ("nested/.env.example/secrets", True),
    )

    for path, is_forbidden in cases:
        assert (_forbidden_reason(path) is not None) is is_forbidden, path


def test_forbidden_matcher_covers_root_and_nested_service_runtime_paths():
    cases = (
        ("services/.runtime.json", True),
        ("services/.runtime-prod.json", True),
        ("services/sub/.runtime-prod.json", True),
        ("services/access.token", True),
        ("services/sub/access.token", True),
        ("services/.gitlab-token", True),
        ("services/sub/.gitlab-token", True),
        ("services/proxy.pid", True),
        ("services/sub/proxy.pid", True),
        ("services/relay.log", True),
        ("services/sub/relay.log", True),
        ("services/gitlab/launch.py", False),
        ("other/.runtime-prod.json", False),
    )

    for path, is_forbidden in cases:
        assert (_forbidden_reason(path) is not None) is is_forbidden, path


def test_git_tree_excludes_private_and_generated_paths():
    forbidden = {
        path: reason
        for path in _tracked_paths()
        if (reason := _forbidden_reason(path)) is not None
    }

    assert not forbidden, "forbidden tracked paths:\n" + "\n".join(
        f"- {path}: {reason}" for path, reason in sorted(forbidden.items())
    )
