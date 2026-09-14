import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PIN = json.loads((ROOT / "examples/osworld-v2/upstream.lock.json").read_text())["code"][
    "commit"
]


def test_verify_checkout_rejects_drift_without_modifying_it(tmp_path):
    source = ROOT / "OSWorld-V2"
    if not (source / ".git").exists():
        pytest.skip("pinned upstream checkout not installed")
    checkout = tmp_path / "upstream"
    subprocess.run(
        ["git", "clone", "--shared", "--quiet", str(source), str(checkout)], check=True
    )
    setup = ["bash", str(ROOT / "runner/setup.sh")]
    result = subprocess.run(
        [*setup, str(checkout)], check=True, capture_output=True, text=True
    )
    assert str(ROOT / "README.md") in result.stdout
    assert "# host-side relay" not in result.stdout
    verify = [*setup, "--verify", str(checkout)]
    assert subprocess.run(verify, capture_output=True).returncode == 0
    upstream_runner = subprocess.check_output(
        ["git", "-C", str(checkout), "show", f"{PIN}:lib_run_single.py"]
    )
    assert (checkout / "lib_run_single.py").read_bytes() == upstream_runner
    backend = "desktop_env/evaluators/backends/openai_backend.py"
    pristine = subprocess.check_output(
        ["git", "-C", str(checkout), "show", f"{PIN}:{backend}"]
    )
    assert (checkout / backend).read_bytes() == pristine
    for relative in (
        "lib_run_single.py",
        "desktop_env/providers/e2b/bridge.py",
        "run.py",
        backend,
    ):
        target = checkout / relative
        original = target.read_bytes()
        changed = original + b"\n# unexpected modification\n"
        target.write_bytes(changed)
        failed = subprocess.run(verify, capture_output=True, text=True)
        assert failed.returncode != 0
        assert "runner/setup.sh" in failed.stderr, relative  # says how to repair
        assert target.read_bytes() == changed
        target.write_bytes(original)
    # --restore reverts every tracked file in the footprint, including a
    # leftover edit to the evaluator backend from an earlier setup.sh version.
    (checkout / backend).write_bytes(pristine + b"\n# legacy edit\n")
    subprocess.run(
        [*setup, "--restore", str(checkout)], check=True, capture_output=True
    )
    status = subprocess.check_output(
        ["git", "-C", str(checkout), "status", "--porcelain"], text=True
    )
    assert status == ""
    subprocess.run([*setup, str(checkout)], check=True, capture_output=True)
    assert subprocess.run(verify, capture_output=True).returncode == 0
    subprocess.run(
        ["git", "-C", str(checkout), "checkout", "--quiet", "HEAD~1"], check=True
    )
    assert subprocess.run(verify, capture_output=True).returncode != 0


def test_setup_patches_the_m3_runner_provider_choices(tmp_path):
    dest = tmp_path / "OSWorld-V2"
    (dest / "scripts" / "python").mkdir(parents=True)
    (dest / "desktop_env" / "providers").mkdir(parents=True)
    (dest / "desktop_env" / "providers" / "__init__.py").write_text(
        '    else:\n        raise NotImplementedError(f"{provider_name} not implemented!")'
    )
    (dest / "desktop_env" / "desktop_env.py").write_text(
        'if self.provider_name in {"docker", "aws", "gcp", "azure", "aliyun", "volcengine"}:\n'
        "if self.is_environment_used:\n"
    )
    (dest / "scripts" / "python" / "run_multienv_m3.py").write_text(
        '        "--provider_name", type=str, default="aws", '
        'choices=["aws", "virtualbox", "vmware", "docker", "azure"], help="Provider name"\n'
    )
    script = (ROOT / "runner" / "setup.sh").read_text()
    body = script[
        script.index("apply_adapter_patches() {") : script.index(
            "\n}\n", script.index("apply_adapter_patches() {")
        )
        + 3
    ]
    subprocess.run(
        ["bash", "-c", body + f'\napply_adapter_patches "{dest}"'],
        check=True,
        capture_output=True,
        text=True,
    )
    patched = (dest / "scripts" / "python" / "run_multienv_m3.py").read_text()
    assert (
        'choices=["aws", "virtualbox", "vmware", "docker", "azure", "e2b"]' in patched
    )
    # idempotent
    subprocess.run(
        ["bash", "-c", body + f'\napply_adapter_patches "{dest}"'],
        check=True,
        capture_output=True,
        text=True,
    )
    assert patched == (dest / "scripts" / "python" / "run_multienv_m3.py").read_text()
