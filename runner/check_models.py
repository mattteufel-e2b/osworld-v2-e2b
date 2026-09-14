#!/usr/bin/env python3
"""Check live upstream judge and selected task simulator clients before rollout."""

from __future__ import annotations

import argparse
import ast
import json
import secrets
import sys
import tempfile
from pathlib import Path

from evaluator_model_calls import require_response


def simulator_configs(manifest, tasks_dir):
    """Read the pinned tasks' literal configs without importing service clients."""
    configs = []
    for task in manifest["tasks"]:
        path = tasks_dir / f"task_{task['id']}.py"
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if not isinstance(node, ast.Assign) or not any(
                isinstance(target, ast.Name) and target.id == "user_simulator"
                for target in node.targets
            ):
                continue
            if isinstance(node.value, ast.Dict):
                config = {
                    ast.literal_eval(key): ast.literal_eval(value)
                    for key, value in zip(node.value.keys, node.value.values)
                    if ast.literal_eval(key)
                    not in {"knowledge", "persona", "instruction"}
                }
            else:
                config = ast.literal_eval(node.value)
            if not config or config.get("type") != "llm":
                continue
            if config not in configs:
                configs.append(config)
    return configs


def check_models(
    generate_text, simulator_class, simulator_configs, image_path, image_answer="4827"
):
    # Tasks use budgets as small as five tokens. A generous probe would hide
    # reasoning-only/empty output at the actual evaluator boundary.
    for prompt, images, expected in (
        ("Is two plus two equal to four? Reply only YES or NO.", None, "YES"),
        (
            "Read the four digits in the image. Reply only with those digits.",
            [image_path],
            image_answer,
        ),
    ):
        answer = require_response(
            generate_text(
                prompt,
                image_paths=images,
                options={"max_tokens": 5, "temperature": 0.0},
            )
        )
        if answer.strip().upper() != expected:
            kind = "image" if images else "text"
            raise ValueError(f"judge {kind} check returned an incorrect answer")
    for config in simulator_configs:
        probe_config = {
            **config,
            "knowledge": "My favorite color is blue.",
            "persona": None,
            "instruction": None,
        }
        require_response(
            simulator_class(probe_config).respond("What is your favorite color?")
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--tasks-dir", type=Path, required=True)
    args = parser.parse_args()
    # Run from the pinned checkout in its full evaluator environment.
    sys.path.insert(0, str(Path.cwd()))
    from desktop_env.evaluators.model_client import generate_text
    from desktop_env.user_simulator import LLMUserSimulator
    from PIL import Image, ImageDraw, ImageFont

    configs = simulator_configs(json.loads(args.manifest.read_text()), args.tasks_dir)
    with tempfile.TemporaryDirectory(prefix="osworld-model-check-") as directory:
        image_path = str(Path(directory) / "digits.png")
        image_answer = str(secrets.randbelow(9000) + 1000)
        image = Image.new("RGB", (512, 160), "white")
        ImageDraw.Draw(image).text(
            (40, 30), image_answer, font=ImageFont.load_default(size=96), fill="black"
        )
        image.save(image_path)
        check_models(generate_text, LLMUserSimulator, configs, image_path, image_answer)
    print(
        f"live model check ok: text + image judge, {len(configs)} simulator configuration(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
