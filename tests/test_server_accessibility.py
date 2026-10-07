"""Exercise the fetched server's AT-SPI scheduling without a Linux desktop."""

import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parents[1]


class XmlNode(ElementTree.Element):
    def xpath(self, *args, **kwargs):
        return []


@pytest.fixture
def accessibility(monkeypatch):
    path = ROOT / "template/files/server/src/accessibility.py"
    if not path.exists():
        pytest.skip("run template/fetch_server.sh to install the pinned server")
    source = ast.parse(path.read_text())
    # Only native imports are substituted. Run the real reader functions and
    # synchronization, using stdlib XML for these small traversal fixtures.
    source.body = [
        node
        for node in source.body
        if not (
            isinstance(node, ast.ImportFrom)
            and node.module in {"platform_runtime", "lxml.etree"}
            or isinstance(node, ast.Import)
            and any(alias.name == "lxml.etree" for alias in node.names)
        )
    ]
    source.body.insert(
        0,
        ast.ImportFrom(
            module="__future__", names=[ast.alias(name="annotations")], level=0
        ),
    )
    namespace = {
        "lxml": SimpleNamespace(
            etree=SimpleNamespace(
                Element=lambda tag, nsmap=None, **kwargs: XmlNode(tag, **kwargs),
                tostring=ElementTree.tostring,
            )
        ),
        "HAS_PYATSPI": True,
        "pyatspi": SimpleNamespace(
            Registry=SimpleNamespace(getDesktop=lambda _: ["one", "two"])
        ),
    }
    exec(compile(ast.fix_missing_locations(source), str(path), "exec"), namespace)
    monkeypatch.setattr(namespace["platform"], "system", lambda: "Linux")
    namespace["get_libreoffice_version"] = lambda: (7, 3)
    namespace["has_active_terminal"] = lambda desktop: True
    namespace["create_atspi_node"] = lambda node, depth=0: XmlNode(
        "application", name=str(node)
    )
    return namespace


def test_linux_tree_keeps_registry_and_application_reads_on_one_thread(accessibility):
    owner = threading.get_ident()
    threads = []

    def read_node(name, depth=0):
        threads.append(threading.get_ident())
        return XmlNode("application", name=name)

    accessibility["create_atspi_node"] = read_node
    xml = accessibility["build_accessibility_tree"]()
    assert {node.attrib["name"] for node in ElementTree.fromstring(xml)} == {
        "one",
        "two",
    }
    assert threads == [owner, owner]


@pytest.mark.parametrize(
    "first_reader", ["build_accessibility_tree", "get_terminal_output"]
)
@pytest.mark.parametrize("blocked_at", ["registry", "node"])
def test_linux_tree_and_terminal_reads_cannot_overlap(
    accessibility, first_reader, blocked_at
):
    entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    overlap = threading.Event()
    owner = None

    def access(stage):
        nonlocal owner
        current = threading.get_ident()
        if current != owner and entered.is_set() and not release.is_set():
            overlap.set()
        if stage == blocked_at and not entered.is_set():
            owner = current
            entered.set()
            assert release.wait(2), "test did not release the blocked native read"

    def registry(index):
        access("registry")
        return ["one"]

    def node(name, depth=0):
        access("node")
        return XmlNode("application", name=str(name))

    accessibility["pyatspi"].Registry.getDesktop = registry
    accessibility["create_atspi_node"] = node
    second_reader = (
        "get_terminal_output"
        if first_reader == "build_accessibility_tree"
        else "build_accessibility_tree"
    )

    def second():
        second_started.set()
        return accessibility[second_reader]()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(accessibility[first_reader])
        try:
            assert entered.wait(1)
            other = executor.submit(second)
            assert second_started.wait(1)
            assert not overlap.wait(0.1), "AT-SPI calls overlapped across readers"
        finally:
            release.set()
        first.result(timeout=1)
        other.result(timeout=1)


def test_native_read_error_does_not_block_the_next_request(accessibility):
    def fail(index):
        raise RuntimeError("native read failed")

    accessibility["pyatspi"].Registry.getDesktop = fail
    with pytest.raises(RuntimeError, match="native read failed"):
        accessibility["build_accessibility_tree"]()
    accessibility["pyatspi"].Registry.getDesktop = lambda _: ["one"]
    completed = threading.Event()

    def read_again():
        accessibility["get_terminal_output"]()
        completed.set()

    thread = threading.Thread(target=read_again, daemon=True)
    thread.start()
    thread.join(timeout=1)
    assert completed.is_set(), "failed request left AT-SPI serialization locked"
