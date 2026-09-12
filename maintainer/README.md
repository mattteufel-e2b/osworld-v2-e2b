# Maintainer validation ladder

Maintainer-only: not required to run the benchmark (that path lives entirely under
`runner/`). These scripts qualify a guest template build before it ships: they exercise every
environment path of all 108 tasks with **no model calls** (`OSWORLD_EVAL_MODEL_MODE=stub`),
fail closed at any evaluator model boundary, and produce the no-model receipt that the
optional `REQUIRE_NO_MODEL_COVERAGE=1` gate in `runner/run_agent_parallel.sh` consumes.

| Script | Purpose |
|---|---|
| `validate_parallel.sh` | All manifest tasks, one worker each (80 max), aggregate gate |
| `run_path_task.sh` | One no-model task on its own namespaced relay (worker for the above) |
| `validate.sh` | Sequential variant, `VALIDATION_RUNS` passes over a manifest |
| `harness.py` | The no-agent rollout: reset, observe, fail-closed evaluate, receipt |
| `no_model.py`, `readiness.py` | Evaluator stubs and bounded observation retries used by the harness |
| `profile_resources.sh` | Ladder rung 7: publish measured CPU/RAM/disk from evidence |

Relay start-up, watchdog and process-group teardown are shared with the benchmark path
through `runner/worker_lib.sh`; a lifecycle fix there applies to both.
