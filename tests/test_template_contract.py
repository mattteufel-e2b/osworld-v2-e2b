import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
from receipt_safety import RETRYABLE_ERROR_CAUSES  # noqa: E402


def test_reaper_uses_clone_safe_startup_launcher():
    template = (ROOT / "template" / "template.ts").read_text()
    launcher = (ROOT / "template" / "files" / "reaper-launcher.sh").read_text()

    assert "Exec=/usr/local/bin/reaper" in template
    assert ".copy('reaper-launcher.sh', '/usr/local/bin/reaper'" in template
    assert "/opt/REAPER/reaper" in launcher
    assert 'wmctrl -i -c "$about_window_id"' in launcher
    assert "about_seen=1" in launcher
    assert "windowclose" not in launcher


def test_path_harness_uses_namespaced_relay_control_port():
    harness = (ROOT / "runner" / "harness.py").read_text()

    assert 'os.environ.get("OSWORLD_RELAY_PORT_BASE", "0")' in harness
    assert 'f"http://127.0.0.1:{14999 + relay_port_base()}' in harness
    assert 'os.environ.get("TASK_TIMEOUT_SECONDS", "900")' in harness


def test_parallel_validator_owns_proxy_and_namespaces_task_service_ports():
    coordinator = (ROOT / "runner" / "validate_parallel.sh").read_text()

    assert 'PARALLEL_CONCURRENCY="${PARALLEL_CONCURRENCY:-80}"' in coordinator
    assert 'if [ "$PARALLEL_CONCURRENCY" -gt 80 ]' in coordinator
    assert 'HOSTMAP_PORT="8090"' in coordinator
    assert 'task_id" = "082"' in coordinator
    assert 'task_service_ports="3000:3000"' in coordinator
    assert 'OSWORLD_TASK_SERVICE_PORTS="$task_service_ports"' in coordinator
    assert "OSWORLD_TASK_082_HOST_PORT" not in coordinator


def test_sequential_validator_owns_host_proxy_for_whole_run():
    validator = (ROOT / "runner" / "validate.sh").read_text()

    assert 'HOSTMAP_PORT="8090"' in validator
    assert '$UV python "$SERVICES_DIR/hostmap_proxy.py"' in validator
    assert "trap cleanup_all EXIT INT TERM" in validator


def test_website_fanout_accepts_large_state_and_reloads_config():
    launcher = (ROOT / "services" / "websites" / "launch.py").read_text()

    assert "client_max_body_size 16m;" in launcher
    assert "--force-recreate fleet_fanout" in launcher


def test_host_proxy_is_restartable_and_has_parallel_backlog():
    proxy = (ROOT / "services" / "hostmap_proxy.py").read_text()

    assert "allow_reuse_address = True" in proxy
    assert "request_queue_size = 128" in proxy


def test_absolute_chrome_launches_get_software_webgl_flags():
    template = (ROOT / "template" / "template.ts").read_text()
    chrome = (ROOT / "template" / "files" / "google-chrome-shim.sh").read_text()

    assert "ln -sfn /usr/local/bin/google-chrome /usr/bin/google-chrome" in template
    assert "--use-angle=swiftshader" in chrome
    assert "--enable-unsafe-swiftshader" in chrome
    assert "--ignore-gpu-blocklist" in chrome


def test_fetch_server_applies_every_committed_patch_to_the_pinned_checkout():
    fetch = (ROOT / "template" / "fetch_server.sh").read_text()
    build = (ROOT / "template" / "build.ts").read_text()
    lock = json.loads(
        (ROOT / "examples" / "osworld-v2" / "upstream.lock.json").read_text()
    )
    patches = sorted((ROOT / "patches").glob("*.patch"))

    server_commit = "a3cc3f0c64e463f020d1a44780307e9b46cbcab1"
    assert lock["server_code"] == {
        "repository": "xlang-ai/osworld-server",
        "commit": server_commit,
    }
    assert 'lock["server_code"]["commit"]' in fetch
    assert "lock.server_code.commit" in build
    assert "lock.release" in build
    assert [patch.name for patch in patches] == [
        "osworld-server-atspi-guards.patch",
        "osworld-server-runtime-reliability.patch",
    ]
    assert 'PATCH_DIR="$REPO_ROOT/patches"' in fetch
    assert 'for patch_file in "$PATCH_DIR"/*.patch; do' in fetch
    assert 'patch -p1 -s < "$patch_file"' in fetch


def test_server_runtime_patch_adds_exact_path_fallback_and_quiet_recording():
    patch = (ROOT / "patches" / "osworld-server-runtime-reliability.patch").read_text()

    assert "def _process_has_exact_path_argument(path: Path) -> bool:" in patch
    assert "_process_has_exact_path_argument(path_obj)" in patch
    assert re.search(
        r'^\+\s+"-hide_banner",\n\+\s+"-nostats",\n'
        r'\+\s+"-loglevel",\n\+\s+"error",$',
        patch,
        re.MULTILINE,
    )


def test_public_docs_and_scripts_use_only_standalone_repository_paths():
    tracked = (
        subprocess.check_output(
            [
                "git",
                "-C",
                str(ROOT),
                "ls-files",
                "-z",
                "*.md",
                "*.sh",
                "*.py",
                "*.ts",
                ":(exclude)tests/**",
            ]
        )
        .decode()
        .split("\0")
    )
    violations: list[str] = []

    for relative_path in filter(None, tracked):
        text = (ROOT / relative_path).read_text()
        if "env-registry" in text or "rewrites/osworld-v2" in text:
            violations.append(relative_path)
        if re.search(r"(?m)^\s*(?:\$\s*)?cd\s+\.\./", text):
            violations.append(relative_path)

    assert not violations, "non-standalone paths: " + ", ".join(sorted(set(violations)))


def test_quick_start_installs_node_dependencies_before_template_commands():
    readme = (ROOT / "README.md").read_text()

    install = "npm --prefix template ci --ignore-scripts"
    typecheck = "npm --prefix template run typecheck"
    build = "npm --prefix template run build"
    assert "Node >=20.18.1" in readme
    assert readme.index(install) < readme.index(typecheck) < readme.index(build)


def test_runner_scripts_do_not_swallow_assignments_at_line_boundaries():
    violations: list[str] = []

    for script in sorted((ROOT / "runner").glob("*.sh")):
        for line_number, line in enumerate(script.read_text().splitlines(), start=1):
            code_before_comment, separator, comment = line.partition("#")
            if (
                separator
                and code_before_comment.strip()
                and re.search(r"[A-Z][A-Z0-9_]*=", comment)
            ):
                violations.append(f"{script.relative_to(ROOT)}:{line_number}")
            if re.search(
                r'^[A-Z][A-Z0-9_]*=.*"[A-Z][A-Z0-9_]*=',
                code_before_comment.strip(),
            ):
                violations.append(f"{script.relative_to(ROOT)}:{line_number}")

    assert not violations, (
        "comment-swallowed or concatenated assignments: "
        + ", ".join(sorted(set(violations)))
    )


def test_ci_guards_clean_clone_quick_start_contracts():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "find runner services template -type f -name '*.sh'" in workflow
    assert "bash -n" in workflow
    assert "runner/setup.sh --preflight" in workflow
    assert "runner/setup.sh\n" in workflow
    assert "uv run --locked pytest -q" in workflow
    assert "pytest -q" in workflow
    assert "npm ci --ignore-scripts" in workflow
    assert "npm run typecheck" in workflow


def test_setup_preflight_validates_local_inputs_without_calling_git(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    git_called = tmp_path / "git-called"
    fake_git = fake_bin / "git"
    fake_git.write_text(f'#!/bin/sh\ntouch "{git_called}"\nexit 97\n')
    fake_git.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = subprocess.run(
        ["bash", str(ROOT / "runner" / "setup.sh"), "--preflight"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "preflight ok" in result.stdout
    assert "d578d2d4e0dc82b43e270fdaa7fa89d9708cd154" in result.stdout
    assert not git_called.exists()


def test_release_lock_pins_service_sources_and_wrapper_images_by_digest():
    lock = json.loads(
        (ROOT / "examples" / "osworld-v2" / "upstream.lock.json").read_text()
    )
    launcher = (ROOT / "services" / "gitlab" / "launch.py").read_text()

    assert lock["gitlab_code"] == {
        "repository": "Task-Web/gitlab",
        "commit": "8655d651722f4254e59e813de9f68a6732ea525c",
    }
    assert lock["websites_code"] == {
        "repository": "Task-Web/OSWorld-web",
        "commit": "90ec2218f7747b15fe5117cdbe59b8978446ab9c",
    }
    assert lock["code"]["repository"] == "xlang-ai/OSWorld-V2"
    assert re.fullmatch(r"[0-9a-f]{40}", lock["code"]["commit"])
    assert lock["server_code"]["repository"] == "xlang-ai/osworld-server"
    assert re.fullmatch(r"[0-9a-f]{40}", lock["server_code"]["commit"])
    assert lock["tasks_data"]["repository"] == "xlangai/osworld_v2_tasks"
    assert re.fullmatch(r"[0-9a-f]{40}", lock["tasks_data"]["revision"])
    assert lock["tasks_data"]["hash_manifest"] == "examples/osworld-v2/task-hashes.json"
    task_hashes = ROOT / lock["tasks_data"]["hash_manifest"]
    assert task_hashes.is_file()
    assert (
        hashlib.sha256(task_hashes.read_bytes()).hexdigest()
        == (lock["tasks_data"]["manifest_sha256"])
    )
    assert re.fullmatch(r"[0-9a-f]{64}", lock["tasks_data"]["manifest_sha256"])
    assert lock["tasks_data"]["task_count"] == 108
    assert lock["assets_data"]["repository"] == "xlangai/osworld_v2_assets_gated"
    assert re.fullmatch(r"[0-9a-f]{40}", lock["assets_data"]["revision"])
    assert set(lock["service_images"]) == {
        "fanout",
        "gitlab",
        "gitlab_init",
        "gitlab_runner",
    }
    for image in lock["service_images"].values():
        assert re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", image)
    for name in ("fanout", "gitlab", "gitlab_init", "gitlab_runner"):
        assert f"fl.service_image('{name}')" in launcher
    assert "nginx:alpine" not in launcher
    assert "gitlab/gitlab-ce:18.7.0-ce.0" not in launcher


def test_release_lock_validator_rejects_missing_or_mutable_gitlab_pins(tmp_path):
    valid = json.loads(
        (ROOT / "examples" / "osworld-v2" / "upstream.lock.json").read_text()
    )
    cases = {
        "missing-source": {
            key: value for key, value in valid.items() if key != "gitlab_code"
        },
        "mutable-commit": {
            **valid,
            "gitlab_code": {"repository": "Task-Web/gitlab", "commit": "main"},
        },
        "mutable-image": {
            **valid,
            "service_images": {
                **valid.get("service_images", {}),
                "gitlab": "gitlab/gitlab-ce:18.7.0-ce.0",
            },
        },
    }

    for name, payload in cases.items():
        candidate = tmp_path / f"{name}.json"
        candidate.write_text(json.dumps(payload))
        result = subprocess.run(
            ["python3", str(ROOT / "services" / "release_lock.py"), str(candidate)],
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode != 0, name
        assert "release lock invalid" in result.stderr, name


def test_release_lock_validator_rejects_missing_or_mutable_website_pins(tmp_path):
    valid = json.loads(
        (ROOT / "examples" / "osworld-v2" / "upstream.lock.json").read_text()
    )
    cases = {
        "missing-source": {
            key: value for key, value in valid.items() if key != "websites_code"
        },
        "mutable-commit": {
            **valid,
            "websites_code": {
                "repository": "Task-Web/OSWorld-web",
                "commit": "v2026.08.08",
            },
        },
        "wrong-repository": {
            **valid,
            "websites_code": {
                "repository": "somewhere/else",
                "commit": "a" * 40,
            },
        },
    }

    for name, payload in cases.items():
        candidate = tmp_path / f"websites-{name}.json"
        candidate.write_text(json.dumps(payload))
        result = subprocess.run(
            ["python3", str(ROOT / "services" / "release_lock.py"), str(candidate)],
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode != 0, name
        assert "release lock invalid" in result.stderr, name


def test_release_lock_validator_rejects_invalid_primary_contract_fields(tmp_path):
    valid = json.loads(
        (ROOT / "examples" / "osworld-v2" / "upstream.lock.json").read_text()
    )
    cases = {
        "wrong-suite": {**valid, "suite": "osworld"},
        "mutable-release": {**valid, "release": "latest"},
        "mutable-code": {**valid, "code": {**valid["code"], "commit": "main"}},
        "missing-server": {
            key: value for key, value in valid.items() if key != "server_code"
        },
        "mutable-tasks": {
            **valid,
            "tasks_data": {**valid["tasks_data"], "revision": "v2026.08.08"},
        },
        "bad-task-manifest": {
            **valid,
            "tasks_data": {**valid["tasks_data"], "manifest_sha256": "not-a-digest"},
        },
        "mutable-assets": {
            **valid,
            "assets_data": {**valid["assets_data"], "revision": "main"},
        },
    }

    for name, payload in cases.items():
        candidate = tmp_path / f"primary-{name}.json"
        candidate.write_text(json.dumps(payload))
        result = subprocess.run(
            ["python3", str(ROOT / "services" / "release_lock.py"), str(candidate)],
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode != 0, name
        assert "release lock invalid" in result.stderr, name


def test_release_lock_validator_rejects_malformed_or_wrong_image_names(tmp_path):
    valid = json.loads(
        (ROOT / "examples" / "osworld-v2" / "upstream.lock.json").read_text()
    )
    digest = "a" * 64
    malformed = {
        "colon-only": ("fanout", f":@sha256:{digest}"),
        "url-prefix": (
            "gitlab",
            f"https://registry.example/gitlab/gitlab-ce@sha256:{digest}",
        ),
        "uppercase-name": ("gitlab_init", f"DOCKER@sha256:{digest}"),
        "uppercase-digest": ("fanout", f"nginx@sha256:{'A' * 64}"),
        "bracketed-name": ("gitlab_runner", f"[gitlab/gitlab-runner]@sha256:{digest}"),
        "wrong-fanout-name": ("fanout", f"library/nginx@sha256:{digest}"),
        "wrong-gitlab-name": ("gitlab", f"gitlab/gitlab-ee@sha256:{digest}"),
        "wrong-init-name": ("gitlab_init", f"library/docker@sha256:{digest}"),
        "wrong-runner-name": ("gitlab_runner", f"gitlab/runner@sha256:{digest}"),
    }

    for case, (image_name, image) in malformed.items():
        candidate = tmp_path / f"{case}.json"
        candidate.write_text(
            json.dumps(
                {
                    **valid,
                    "service_images": {**valid["service_images"], image_name: image},
                }
            )
        )
        result = subprocess.run(
            ["python3", str(ROOT / "services" / "release_lock.py"), str(candidate)],
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode != 0, case
        assert "release lock invalid" in result.stderr, case


def test_setup_preflight_runs_release_lock_validation_before_any_git_operation():
    setup = (ROOT / "runner" / "setup.sh").read_text()

    validation = 'python3 "$V2ROOT/services/release_lock.py" "$LOCKFILE"'
    assert validation in setup
    assert setup.index(validation) < setup.index('git -C "$DEST" fetch')


def test_upstream_lock_is_not_shadowed_by_checkout_ignore_rule():
    lock = "examples/osworld-v2/upstream.lock.json"

    ignored = subprocess.run(
        ["git", "-C", str(ROOT), "check-ignore", "--quiet", lock],
        check=False,
    )

    assert (ROOT / lock).is_file()
    assert ignored.returncode == 1


def test_xt_owner_rule_is_translated_to_native_nft_skuid():
    template = (ROOT / "template" / "template.ts").read_text()
    shim = (ROOT / "template" / "files" / "iptables-owner-compat.sh").read_text()

    assert ".copy('iptables-owner-compat.sh', '/usr/local/sbin/iptables'" in template
    assert "meta skuid !=" in shim
    assert 'exec /usr/sbin/iptables "$@"' in shim


def test_nested_docker_uses_checksum_pinned_compose_v2_plugin():
    template = (ROOT / "template" / "template.ts").read_text()

    assert (
        "docker/compose/releases/download/v2.40.3/docker-compose-linux-x86_64"
        in template
    )
    assert (
        "dba9d98e1ba5bfe11d88c99b9bd32fc4a0624a30fafe68eea34d61a3e42fd372" in template
    )
    assert "/usr/local/lib/docker/cli-plugins/docker-compose" in template


def test_runtime_apt_installs_preserve_image_managed_configuration():
    template = (ROOT / "template" / "template.ts").read_text()
    apt_compat = (ROOT / "template" / "files" / "apt-get-noninteractive.sh").read_text()

    assert ".copy('apt-get-noninteractive.sh', '/usr/local/sbin/apt-get'" in template
    assert "DEBIAN_FRONTEND=noninteractive" in apt_compat
    assert "UCF_FORCE_CONFFOLD=1" in apt_compat
    assert 'exec /usr/bin/apt-get "$@"' in apt_compat


def test_openboard_snap_contract_maps_to_ubuntu_package_without_snapd():
    template = (ROOT / "template" / "template.ts").read_text()
    snap_compat = (ROOT / "template" / "files" / "snap-openboard-compat.sh").read_text()

    assert "apt-get install -y musescore3 shotcut freecad openboard" in template
    assert "apt-mark hold musescore3 shotcut freecad openboard" in template
    assert "mkdir -p /snap/bin" in template
    assert "ln -sfn /usr/bin/OpenBoard /snap/bin/openboard" in template
    assert ".copy('snap-openboard-compat.sh', '/usr/local/bin/snap'" in template
    assert 'if [ "$1" = "list" ] && [ "${2:-}" = "openboard" ]' in snap_compat
    assert 'if [ "$1" = "install" ] && [ "${2:-}" = "openboard" ]' in snap_compat
    assert "unsupported snap command" in snap_compat


def test_agent_runner_supports_any_openai_compatible_provider_without_secret_logging():
    runner = (ROOT / "runner" / "run_agent.sh").read_text()
    agent = (ROOT / "runner" / "agent_runner.py").read_text()

    assert 'MODEL_BASE_URL="${MODEL_BASE_URL:-' in runner
    assert 'MODEL_API_KEY="${MODEL_API_KEY:-' in runner
    assert 'os.environ["MODEL_BASE_URL"]' in agent
    assert "os.environ['MODEL_API_KEY']" in agent
    assert "OPENROUTER_API_KEY required" not in runner
    assert "class CompatiblePromptAgent" in agent
    assert "No secrets are logged" in agent


def test_agent_runner_can_use_the_release_m3_scaffold_and_anthropic_transport():
    runner = (ROOT / "runner" / "run_agent.sh").read_text()
    agent = (ROOT / "runner" / "agent_runner.py").read_text()

    assert 'AGENT_KIND="${AGENT_KIND:-prompt}"' in runner
    assert 'base_url=os.environ["MODEL_BASE_URL"]' in agent
    assert 'api_key=os.environ["MODEL_API_KEY"]' in agent
    assert "from mm_agents.m3 import M3Agent" in agent
    assert 'choices=("prompt", "m3")' in agent
    assert "M3Agent(" in agent
    assert "max_tokens=8192" in agent
    assert "max_trajectory_length=10" in agent


def test_full_agent_coordinator_bounds_sandboxes_and_namespaces_task_service_ports():
    coordinator = (ROOT / "runner" / "run_agent_parallel.sh").read_text()
    aggregator = (ROOT / "runner" / "aggregate_agent.py").read_text()

    assert 'PARALLEL_CONCURRENCY="${PARALLEL_CONCURRENCY:-80}"' in coordinator
    assert 'if [ "$PARALLEL_CONCURRENCY" -gt 80 ]' in coordinator
    assert 'HOSTMAP_PORT="8090"' in coordinator
    assert 'task_id" = "082"' in coordinator
    assert 'task_service_ports="3000:3000"' in coordinator
    assert "OSWORLD_TASK_082_HOST_PORT" not in coordinator
    assert 'task_service_ports=""' in coordinator
    assert 'AGENT_RETRY_ATTEMPTS="${AGENT_RETRY_ATTEMPTS:-0}"' in coordinator
    # Retry-candidate selection is delegated to a standalone script rather than
    # a heredoc embedded in a process substitution: macOS system bash (3.2)
    # mis-parses that construct and silently drops the retry wave.
    assert (
        'python3 "$HERE/retry_candidates.py" "$MANIFEST" "$RAW_DIR/workers"'
        in coordinator
    )
    assert "task-timeout" in RETRYABLE_ERROR_CAUSES
    assert "evaluator-or-agent" not in RETRYABLE_ERROR_CAUSES
    assert 'python3 "$HERE/aggregate_agent.py"' in coordinator
    assert 'python3 "$HERE/model_coverage.py"' in coordinator
    assert 'REQUIRE_NO_MODEL_COVERAGE="${REQUIRE_NO_MODEL_COVERAGE:-0}"' in coordinator
    assert 'AGENT_RETRY_CONCURRENCY="${AGENT_RETRY_CONCURRENCY:-4}"' in coordinator
    assert (
        'AGENT_START_STAGGER_SECONDS="${AGENT_START_STAGGER_SECONDS:-0.25}"'
        in coordinator
    )
    assert (
        "for ((attempt=1; attempt <= AGENT_RETRY_ATTEMPTS; attempt++))" in coordinator
    )
    assert '"path_ok": sum(record.get("path_status") == "OK"' in aggregator
    assert '"evaluator_ran_count": sum(' in aggregator
    assert '"scored_tasks": len(scores)' in aggregator
    assert '"binary_successes": sum(score == 1.0 for score in scores)' in aggregator
    assert '"binary_accuracy": (' in aggregator
    assert '"partial_score": (' in aggregator
    assert '"agent_kind": agent_kind' in aggregator
    assert '"model_transport": expected_model_transport' in aggregator
    assert '"reasoning": {' in aggregator
    assert '"evaluator": {' in aggregator


def test_agent_coordinator_can_opt_literal_port_task_into_a_sample_wave():
    coordinator = (ROOT / "runner" / "run_agent_parallel.sh").read_text()
    aggregator = (ROOT / "runner" / "aggregate_agent.py").read_text()

    assert 'RUN_TASK_082_CONCURRENT="${RUN_TASK_082_CONCURRENT:-1}"' in coordinator
    assert 'if [ "$task_id" = "082" ]; then' in coordinator
    assert 'task_service_ports="3000:3000"' in coordinator
    assert 'if [ "$RUN_TASK_082_CONCURRENT" != "1" ]' in coordinator
    assert '"task_082_concurrent": task_082_concurrent' in aggregator


def test_no_model_aggregate_distinguishes_full_paths_from_model_boundaries():
    parallel = (ROOT / "runner" / "validate_parallel.sh").read_text()
    sequential = (ROOT / "runner" / "validate.sh").read_text()

    assert "MODEL_BOUNDARY_PASS" in parallel
    assert '"model_boundary_passes"' in parallel
    assert '"validated_tasks"' in parallel
    assert "MODEL_BOUNDARY_PASS" in sequential
    assert "model_boundary_passes=" in sequential
    assert "validated_tasks=" in sequential


def test_task_082_uses_the_literal_canonical_port_3000():
    task_path = (
        Path(os.environ.get("OSWORLD_TASKS_DIR", ROOT / "tasks")) / "task_082.py"
    )
    if not task_path.is_file():
        import pytest

        pytest.skip("gated task data not downloaded (tasks/); see README prerequisites")
    task = task_path.read_text()

    assert "AWS_PORT = 3000" in task
    assert "OSWORLD_TASK_082_HOST_PORT" not in task
    assert 'return f"http://{host}:{AWS_PORT}"' in task
    assert 'return f"http://localhost:{AWS_PORT}' in task


def test_agent_receipt_records_reasoning_and_evaluator_provenance_without_keys():
    agent = (ROOT / "runner" / "agent_runner.py").read_text()

    assert '"thinking_mode": os.environ.get("M3_THINKING_MODE") or None' in agent
    assert '"thinking_budget": _positive_int_env("M3_THINKING_BUDGET")' in agent
    assert '"eval_model": os.environ.get("OSWORLD_EVAL_MODEL_NAME") or None' in agent
    assert (
        '"eval_provider": os.environ.get("OSWORLD_EVAL_MODEL_PROVIDER") or None'
        in agent
    )
    assert (
        '"api_key"'
        not in agent.split("receipt = {", 1)[1].split("\n    }", 1)[0].lower()
    )


def test_agent_worker_creates_custom_relay_log_parent_before_redirection():
    runner = (ROOT / "runner" / "run_agent.sh").read_text()

    assert (
        'mkdir -p "$RESULT_DIR" "$(dirname "$OUTPUT")" "$(dirname "$RELAY_LOG")"'
        in runner
    )


def test_agent_worker_canonicalizes_output_paths_before_entering_upstream_checkout():
    runner = (ROOT / "runner" / "run_agent.sh").read_text()

    assert 'RAW_DIR="$(abspath "$RAW_DIR")"' in runner
    assert 'RESULT_DIR="$(abspath "$RESULT_DIR")"' in runner
    assert 'OUTPUT="$(abspath "$OUTPUT")"' in runner
    assert 'RELAY_LOG="$(abspath "$RELAY_LOG")"' in runner


def test_setup_patches_terminal_none_screenshot_before_agent_runs():
    setup = (ROOT / "runner" / "setup.sh").read_text()

    assert "lib_run_single.py" in setup
    assert "terminal observation returned screenshot=None" in setup
    assert "if screenshot_bytes is not None:" in setup
    assert "desktop_env/providers/__init__.py lib_run_single.py" in setup


def test_template_build_smoke_covers_ipv4_and_ipv6_protected_ranges():
    build = (ROOT / "template" / "build.ts").read_text()

    for cidr in ("169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8"):
        assert cidr in build


def test_guest_server_dependency_contract_excludes_broken_anyio_release():
    requirements = {
        name.lower(): version
        for line in (ROOT / "template" / "files" / "server" / "requirements.txt")
        .read_text()
        .splitlines()
        if line and not line.startswith("#")
        for name, separator, version in [line.partition("==")]
        if separator
    }

    assert requirements.get("anyio") == "4.14.2"
