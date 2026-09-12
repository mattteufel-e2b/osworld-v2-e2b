import builtins
import subprocess
import sys

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
from lazy_import import lazy_module  # noqa: E402


def test_lazy_module_defers_execution_until_first_attribute_access(tmp_path):
    # importlib.util.LazyLoader cannot do this: a later plain `import name`
    # touches the module's __spec__, which forces the load immediately.
    (tmp_path / "lazy_probe_mod.py").write_text(
        "import builtins\nbuiltins.LAZY_PROBE_LOADED = True\nVALUE = 42\n"
    )
    sys.path.insert(0, str(tmp_path))
    builtins.LAZY_PROBE_LOADED = False
    try:
        lazy_module("lazy_probe_mod")
        import lazy_probe_mod  # noqa: F401  (the docs.py-style module import)

        assert builtins.LAZY_PROBE_LOADED is False
        assert lazy_probe_mod.VALUE == 42
        assert builtins.LAZY_PROBE_LOADED is True
        assert type(sys.modules["lazy_probe_mod"]).__name__ == "module"
    finally:
        sys.modules.pop("lazy_probe_mod", None)
        sys.path.remove(str(tmp_path))
        del builtins.LAZY_PROBE_LOADED


def test_lazy_module_loads_once_for_repeated_access_through_the_stand_in(tmp_path):
    # Upstream docs.py does `import easyocr` at module level and later calls
    # `easyocr.Reader(...)`: the importer keeps the stand-in bound, so every
    # attribute lookup must hit the loaded namespace, not re-run the package.
    (tmp_path / "lazy_probe_once.py").write_text(
        "import builtins\n"
        "builtins.LAZY_PROBE_LOADS = getattr(builtins, 'LAZY_PROBE_LOADS', 0) + 1\n"
        "class Reader:\n    pass\n"
    )
    sys.path.insert(0, str(tmp_path))
    builtins.LAZY_PROBE_LOADS = 0
    try:
        lazy_module("lazy_probe_once")
        import lazy_probe_once

        readers = {lazy_probe_once.Reader for _ in range(3)}
        assert builtins.LAZY_PROBE_LOADS == 1
        assert len(readers) == 1
        assert lazy_probe_once.Reader is sys.modules["lazy_probe_once"].Reader
    finally:
        sys.modules.pop("lazy_probe_once", None)
        sys.path.remove(str(tmp_path))
        del builtins.LAZY_PROBE_LOADS


def test_lazy_module_keeps_raising_import_error_when_the_real_import_fails(tmp_path):
    (tmp_path / "lazy_probe_broken.py").write_text("raise ImportError('no torch')\n")
    sys.path.insert(0, str(tmp_path))
    try:
        lazy_module("lazy_probe_broken")
        import lazy_probe_broken

        for _ in range(2):  # never KeyError on the second attempt
            with pytest.raises(ImportError, match="no torch"):
                lazy_probe_broken.Reader
    finally:
        sys.modules.pop("lazy_probe_broken", None)
        sys.path.remove(str(tmp_path))


def test_lazy_module_ignores_uninstalled_names():
    lazy_module("definitely_not_installed_probe")
    assert "definitely_not_installed_probe" not in sys.modules


def test_both_runners_keep_torch_out_of_worker_startup():
    for path in ("runner/agent_runner.py", "maintainer/harness.py"):
        source = (ROOT / path).read_text()
        assert 'lazy_module("easyocr")' in source, path
        assert source.index('lazy_module("easyocr")') < source.index(
            "from desktop_env.desktop_env import DesktopEnv"
        ), path
    venv_python = ROOT / "OSWorld-V2" / ".venv" / "bin" / "python"
    if not venv_python.exists():
        return
    result = subprocess.run(
        [
            str(venv_python),
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "from lazy_import import lazy_module; lazy_module('easyocr');"
            "import desktop_env.desktop_env; print('torch' in sys.modules)",
            str(ROOT / "runner"),
        ],
        cwd=ROOT / "OSWorld-V2",
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False", result.stderr
