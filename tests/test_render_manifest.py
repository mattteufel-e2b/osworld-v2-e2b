import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = "osworld-v2-gnome:1df8561f-84b4-4092-a15b-274c40985b35"


def test_render_manifest_can_select_an_exact_task_subset(tmp_path):
    output = tmp_path / "sample.json"
    result = subprocess.run(
        [
            "python3",
            str(ROOT / "runner" / "render_manifest.py"),
            "--source",
            str(ROOT / "validation" / "full-manifest.json"),
            "--template",
            TEMPLATE,
            "--output",
            str(output),
            "--task-id",
            "003",
            "--task-id",
            "082",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads(output.read_text())
    assert [task["id"] for task in manifest["tasks"]] == ["003", "082"]
    assert manifest["template"] == TEMPLATE


def test_render_manifest_rejects_unknown_or_duplicate_task_ids(tmp_path):
    for task_ids in (("999",), ("003", "003")):
        output = tmp_path / ("-".join(task_ids) + ".json")
        command = [
            "python3",
            str(ROOT / "runner" / "render_manifest.py"),
            "--source",
            str(ROOT / "validation" / "full-manifest.json"),
            "--template",
            TEMPLATE,
            "--output",
            str(output),
        ]
        for task_id in task_ids:
            command.extend(("--task-id", task_id))
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode != 0
        assert not output.exists()
