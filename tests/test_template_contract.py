import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
    assert 'task_service_ports="60082:3000"' in coordinator
    assert 'OSWORLD_TASK_SERVICE_PORTS="$task_service_ports"' in coordinator
    assert 'OSWORLD_TASK_082_HOST_PORT="$task_082_host_port"' in coordinator


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
    patches = sorted((ROOT / "patches").glob("*.patch"))

    server_commit = "a3cc3f0c64e463f020d1a44780307e9b46cbcab1"
    assert f'COMMIT="{server_commit}"' in fetch
    assert f"const OSWORLD_SERVER_COMMIT = '{server_commit}'" in build
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


def test_xt_owner_rule_is_translated_to_native_nft_skuid():
    template = (ROOT / "template" / "template.ts").read_text()
    shim = (ROOT / "template" / "files" / "iptables-owner-compat.sh").read_text()

    assert ".copy('iptables-owner-compat.sh', '/usr/local/sbin/iptables'" in template
    assert "meta skuid !=" in shim
    assert 'exec /usr/sbin/iptables "$@"' in shim


def test_nested_docker_uses_checksum_pinned_compose_v2_plugin():
    template = (ROOT / "template" / "template.ts").read_text()

    assert "docker/compose/releases/download/v2.40.3/docker-compose-linux-x86_64" in template
    assert "dba9d98e1ba5bfe11d88c99b9bd32fc4a0624a30fafe68eea34d61a3e42fd372" in template
    assert "/usr/local/lib/docker/cli-plugins/docker-compose" in template


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
    assert 'export OPENAI_BASE_URL="$MODEL_BASE_URL"' in runner
    assert 'export OPENAI_API_KEY="$MODEL_API_KEY"' in runner
    assert "OPENROUTER_API_KEY required" not in runner
    assert "class CompatiblePromptAgent" in agent
    assert "No secrets are logged" in agent


def test_agent_runner_can_use_the_release_m3_scaffold_and_anthropic_transport():
    runner = (ROOT / "runner" / "run_agent.sh").read_text()
    agent = (ROOT / "runner" / "agent_runner.py").read_text()

    assert 'AGENT_KIND="${AGENT_KIND:-prompt}"' in runner
    assert 'export ANTHROPIC_BASE_URL="$MODEL_BASE_URL"' in runner
    assert 'export ANTHROPIC_API_KEY="$MODEL_API_KEY"' in runner
    assert "from mm_agents.m3 import M3Agent" in agent
    assert 'choices=("prompt", "m3")' in agent
    assert "M3Agent(" in agent
    assert "max_tokens=8192" in agent
    assert "max_trajectory_length=10" in agent


def test_full_agent_coordinator_bounds_sandboxes_and_namespaces_task_service_ports():
    coordinator = (ROOT / "runner" / "run_agent_parallel.sh").read_text()

    assert 'PARALLEL_CONCURRENCY="${PARALLEL_CONCURRENCY:-80}"' in coordinator
    assert 'if [ "$PARALLEL_CONCURRENCY" -gt 80 ]' in coordinator
    assert 'HOSTMAP_PORT="8090"' in coordinator
    assert 'task_id" = "082"' in coordinator
    assert 'task_service_ports="$task_082_host_port:3000"' in coordinator
    assert 'OSWORLD_TASK_082_HOST_PORT="$task_082_host_port"' in coordinator
    assert 'task_service_ports=""' in coordinator
    assert 'AGENT_RETRY_ATTEMPTS="${AGENT_RETRY_ATTEMPTS:-2}"' in coordinator
    assert 'AGENT_RETRY_CONCURRENCY="${AGENT_RETRY_CONCURRENCY:-4}"' in coordinator
    assert 'AGENT_START_STAGGER_SECONDS="${AGENT_START_STAGGER_SECONDS:-0.25}"' in coordinator
    assert "for ((attempt=1; attempt <= AGENT_RETRY_ATTEMPTS; attempt++))" in coordinator
    assert 'record.get("path_status") != "OK"' in coordinator
    assert 'summary["path_ok"] == len(expected_ids)' in coordinator
    assert 'summary["evaluator_ran_count"] == len(expected_ids)' in coordinator
    assert 'summary["scored_tasks"] == len(expected_ids)' in coordinator
    assert '"binary_successes": sum(score == 1.0 for score in scores)' in coordinator
    assert '"binary_accuracy": (' in coordinator
    assert '"partial_score": (' in coordinator
    assert '"agent_kind": agent_kind' in coordinator
    assert '"model_transport": model_base_url' in coordinator
    assert '"reasoning": {' in coordinator
    assert '"evaluator": {' in coordinator


def test_agent_coordinator_can_opt_literal_port_task_into_a_sample_wave():
    coordinator = (ROOT / "runner" / "run_agent_parallel.sh").read_text()

    assert 'RUN_TASK_082_CONCURRENT="${RUN_TASK_082_CONCURRENT:-1}"' in coordinator
    assert 'if [ "$task_id" = "082" ]; then' in coordinator
    assert 'task_service_ports="$task_082_host_port:3000"' in coordinator
    assert 'if [ "$RUN_TASK_082_CONCURRENT" != "1" ]' in coordinator
    assert '"task_082_concurrent": task_082_concurrent' in coordinator


def test_task_082_separates_host_relay_port_from_guest_browser_port():
    task_path = ROOT / "tasks" / "task_082.py"
    if not task_path.is_file():
        import pytest

        pytest.skip("gated task data not downloaded (tasks/); see README prerequisites")
    task = task_path.read_text()

    assert 'OSWORLD_TASK_082_HOST_PORT' in task
    assert 'return f"http://{host}:{_host_aws_port()}"' in task
    assert 'return f"http://localhost:{AWS_PORT}' in task


def test_agent_receipt_records_reasoning_and_evaluator_provenance_without_keys():
    agent = (ROOT / "runner" / "agent_runner.py").read_text()

    assert '"thinking_mode": os.environ.get("M3_THINKING_MODE") or None' in agent
    assert '"thinking_budget": _positive_int_env("M3_THINKING_BUDGET")' in agent
    assert '"eval_model": os.environ.get("OSWORLD_EVAL_MODEL_NAME") or None' in agent
    assert '"eval_provider": os.environ.get("OSWORLD_EVAL_MODEL_PROVIDER") or None' in agent
    assert '"api_key"' not in agent.split('receipt = {', 1)[1].split('\n    }', 1)[0].lower()


def test_agent_worker_creates_custom_relay_log_parent_before_redirection():
    runner = (ROOT / "runner" / "run_agent.sh").read_text()

    assert 'mkdir -p "$RESULT_DIR" "$(dirname "$OUTPUT")" "$(dirname "$RELAY_LOG")"' in runner


def test_agent_worker_canonicalizes_output_paths_before_entering_upstream_checkout():
    runner = (ROOT / "runner" / "run_agent.sh").read_text()

    assert 'RAW_DIR="$(abspath "$RAW_DIR")"' in runner
    assert 'RESULT_DIR="$(abspath "$RESULT_DIR")"' in runner
    assert 'OUTPUT="$(abspath "$OUTPUT")"' in runner
    assert 'RELAY_LOG="$(abspath "$RELAY_LOG")"' in runner


def test_setup_patches_terminal_none_screenshot_before_agent_runs():
    setup = (ROOT / "runner" / "setup.sh").read_text()

    assert "lib_run_single.py" in setup
    assert 'terminal observation returned screenshot=None' in setup
    assert 'if screenshot_bytes is not None:' in setup
    assert 'desktop_env/providers/__init__.py lib_run_single.py' in setup


def test_agent_worker_routes_user_simulator_to_explicit_compatible_model():
    runner = (ROOT / "runner" / "run_agent.sh").read_text()
    agent = (ROOT / "runner" / "agent_runner.py").read_text()

    assert 'export OSWORLD_USER_SIM_PROVIDER="openai_compatible"' in runner
    assert 'export OSWORLD_USER_SIM_BASE_URL="$EVAL_MODEL_BASE_URL"' in runner
    assert 'export OSWORLD_USER_SIM_MODEL="${USER_SIM_MODEL:-${EVAL_MODEL:-$MODEL}}"' in runner
    assert '"user_sim_model": os.environ.get("OSWORLD_USER_SIM_MODEL") or None' in agent


def test_template_build_smoke_covers_ipv4_and_ipv6_protected_ranges():
    build = (ROOT / "template" / "build.ts").read_text()

    for cidr in ("169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8"):
        assert cidr in build
