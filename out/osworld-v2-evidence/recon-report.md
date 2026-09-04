# OSWorld 2.0 gated-task recon report

Release: `osworld-v2-2026.08.08`. Tasks dataset `xlangai/osworld_v2_tasks` @
`0ec2aa30344a28103a33c89c6588c3437bfcdca1` (108 `task_*.py` files, verified against
`examples/osworld-v2/task-hashes.json`). Assets dataset `xlangai/osworld_v2_assets_gated` @
`402754cc7bd74690168c46ef0c58c375542c4afe` (3186 payload files plus `.source.json`, ~4.2GB).
Both were downloaded into this
repository's ignored `tasks/` workspace (`git status --porcelain` had zero entries under that
path). No task prompts, evaluator source, or asset content are reproduced below — counts, task
ids, domain/app tags, and endpoint names only.

## 1. What listens on guest ports 3000 and 8000

Neither port is a fixed, always-on daemon baked into the base guest image for every task. Both are
**task-specific, ephemeral HTTP servers**, and the harness reaches them in two different ways
depending on the task:

- **Port 8000** (4 tasks: `task_074`, `task_075`, `task_078`, `task_089`) — the task instructs the
  *agent* to build and serve a small web project on `localhost:8000` as part of completing the
  task; nothing pre-listens there. The evaluator checks it by executing a command **inside the
  guest** over the existing control channel (the same mechanism `PythonController`/
  `SetupController` already use against port 5000) — an in-guest `python3 -c "urllib.request...
  localhost:8000"` / CDP screenshot of the guest's own Chrome tab. The harness process itself never
  opens a socket to `vm_ip:8000`.
- **Port 3000** (2 tasks: `task_082`, `task_086`) — two different patterns:
  - `task_086` opens `localhost:3000` in a guest Chrome tab as one of several dev-server tabs
    launched during setup (guest-internal only, same as the 8000 pattern above).
  - `task_082` is the one confirmed case of a **direct host-to-guest connection**: its setup phase
    waits on `http://<vm_ip>:3000/health` and its evaluator does `requests.get` against
    `http://<vm_ip>:3000` from the harness process itself (a task-local mocked in-guest service,
    using the same cookie/state-save pattern as the mocked-website fleet, but hosted in-guest
    rather than behind the external Caddy fleet). This is a real host-process socket to
    `vm_ip:3000`, not a channel-relayed guest command.

Net effect for the E2B relay: ports 3000 and 8000 must both be genuinely reachable from the host
process (not just tunnelable via the port-5000 control channel), because at least one task
(`task_082`) depends on a direct host→guest socket on 3000, even though most 3000/8000 touches
(`task_074/075/078/086/089`) only need in-guest command execution. This matches the public
checkout's own AWS security-group notes (`docs/PROVIDER_SETUP.md`,
`docs/OSWORLD_SETUP_GUIDELINE.md`, `docs/MIGRATING_FROM_OSWORLD_V1.md`), which list 3000 and 8000
generically as "V2 task service port" and require them open from the host VPC CIDR — consistent
with what the task code actually does. No guest-image Packer/Ansible definitions with dedicated
3000/8000 service units were found in the public checkout to search against (none exist in this
release; the ports are purely task-driven).

## 2. Task loading and dispatch mechanism

- **Loading**: `task_loader.load_task_from_file` dynamically imports each `task_NNN.py` as a fresh
  module (`importlib.util.spec_from_file_location` + `exec_module`), then instantiates it via
  `get_task()` / `TASK_CLASS` / `Task` / the first `BaseTask` subclass found in the module. Task
  code runs as arbitrary host-side Python at import time, not only inside `setup()`/`evaluate()`.
- **Dispatch**: `DesktopEnv._has_custom_setup`/`_has_custom_evaluate` (in `desktop_env.py`) decide
  between the overridden-method path and the declarative `config`/`evaluator`-dict path using an
  identity check: `task_config.__class__.setup is not BaseTask.setup` (same for `evaluate`).
  **Dataset-verified**: all 108/108 task files in this release define their own `setup` and
  `evaluate` methods — 0 are pure-declarative (JSON-config-only) tasks. Every task in this dataset
  takes the custom-code dispatch path.
- **Mid-task evaluate()**: confirmed via `lib_run_single.py` that `evaluate()` is *not* always
  end-of-trajectory-only. Two distinct mid-task mechanisms exist:
  1. `MultiPhaseTask.get_phases()` (`desktop_env/task_base.py`) — a task can define N phases, each
     with its own instruction/setup/evaluate, run sequentially on the same live environment, with
     optional `gate`/`gate_min_score` early-stop between phases. **Dataset-verified**: exactly 1 of
     108 tasks (`task_069`) subclasses `MultiPhaseTask` / defines `get_phases()`; the other 107 use
     the single setup→run→evaluate lifecycle.
  2. Opt-in inline checkpoint evaluation at specific step numbers (`checkpoint_eval_mode`/
     `checkpoint_steps` CLI args) calls `env.evaluate()` mid-trajectory into separate
     `result_step_N.{txt,json}` files without disturbing the main run. Gated per-task by the
     `intermediate_eval_safe` `BaseTask` field (default `True`); the runner records a
     `skipped`/`intermediate_eval_safe_false` entry when a task opts out. This is orthogonal to
     `MultiPhaseTask` and available to any task.
- **Checkpoint evaluators / LLM judge** (public-code, not gated): the harness's LLM-judge helpers
  live in `desktop_env/evaluators/metrics/llm_metrics.py` (`compare_images_with_llm`,
  `compare_image_edit_with_llm`, `compare_multiple_images_with_llm`, `compare_text_with_llm`,
  `compare_answers_with_llm`). They touch the guest only through **existing** getters (screenshots,
  file contents, browser state already fetched over the control channel/CDP) and then make a
  host-side LLM API call to score — no new guest endpoint or extra guest port. **Dataset-verified**:
  1 of 108 tasks calls these comparator functions by name in this release; the ~11.5%-of-score
  figure in the case doc is presumably an aggregate across the full suite's individual scoring
  checks, not a per-task constant — a precise per-task checkpoint count was not extractable from
  static grep (evaluator code structures its partial-credit checks with task-specific naming, not
  a uniform "checkpoint" keyword) and is not required to answer this recon question.

## 3. Audio-dependent tasks

8 of 108 tasks reference audio file formats (`.wav`/`.mp3`/`.flac`): task ids
**042, 050, 056, 067, 071, 084, 085, 095**.

- 3 involve REAPER (**050, 084, 085**), 2 involve MuseScore (**067, 071**), 3 involve Shotcut with
  an audio-track component (**042, 056, 095**) — consistent with the case doc's "Expanded
  application set."
- What they check, generically: **all 8 evaluate a rendered/exported audio file**, not live audio
  device or sink state. 3 tasks (**084, 085, 095**) go beyond a file-existence/extension check and
  run actual audio-content analysis on the exported file (`ffprobe`, plus `librosa`/`soundfile` in
  084/085). None of the 8 reference `pactl`/PulseAudio/ALSA/sink names or query live audio-routing
  state — grepped for `pactl|alsa|pulse|sink|snd_|arecord|aplay` across all 8 files with zero hits.
- Implication for the Audio-on-Firecracker P0 spike: evaluation risk is concentrated in "can
  REAPER/MuseScore/Shotcut render/export a file successfully" (offline render path), not in
  "does a live sink report the expected state" — meaningfully de-risks the spike if a null/virtual
  sink is enough to make these apps complete an export.

## 4. Website- and GitLab-dependent tasks

- **Website-dependent** (calls `build_website_url`, i.e. the mocked Next.js/Caddy fleet): 33 of 108
  tasks — ids **5, 7, 8, 13, 15, 16, 18, 19, 20, 21, 31, 38, 39, 43, 46, 50, 52, 56, 57, 58, 60, 65,
  68, 69, 70, 72, 73, 81, 82, 98, 100, 101, 102**.
- **GitLab-dependent** (references `GITLAB_URL`): 1 of 108 tasks — id **26**.

## 5. Twelve validation candidates

Selection rule: exclude tasks already counted as audio/website/GitLab from the "plain" bucket;
maximize distinct `related_apps` domain tags; keep the audio/GitLab/website picks anchored to the
lists above.

**8 plain (no audio dependency), 8 distinct domains:**

| Task | Domain (`related_apps`) | Why |
|---|---|---|
| 003 | gimp | Single-app image-editor domain, no non-default reach-in beyond controller/state checks — a clean baseline for the image-app family. |
| 030 | vscode, terminal | Exercises `execute_python_command` (in-guest command execution reach-in) alongside an IDE-domain task. |
| 004 | libreoffice_impress | Single-app office-suite domain, baseline declarative-style reach-in via controller only. |
| 103 | freecad, pdf_viewer | Covers the CAD/engineering app family (part of the V2 "expanded application set") with a secondary viewer app. |
| 105 | 3d-slicer, file-manager | Exercises `get_file` (guest file retrieval) in the 3D/CAD-adjacent domain. |
| 107 | kicad | Second distinct CAD-tool domain (schematic/PCB), baseline reach-in. |
| 097 | zotero, vscode | Covers the reference-manager app family named in the case doc's expanded app set. |
| 027 | excel | Spreadsheet-domain baseline, distinct from the LibreOffice/WPS office families already covered. |

**1 audio:** task **067** (musescore) — single-domain, isolates the render-to-file audio check
(no other reach-in types mixed in) as the cleanest first validation of the Audio-on-Firecracker
P0 spike's actual pass/fail criterion.

**1 GitLab:** task **026** (vscode, libreoffice_impress, google-chrome) — the richer of the two
GitLab-dependent tasks, additionally exercising `execute_python_command`, `user_simulator`, and CDP
(port 9222), so one validation run stresses GitLab reach-in plus three other reach-in mechanisms
at once.

**2 website:** task **069** (chrome, mailhub, calendar, libreoffice_writer) — the only
`MultiPhaseTask` in the release, combined with website reach-in and CDP; validates the mid-task
multi-phase `evaluate()` path (item 2 above) end-to-end. Task **005** (chrome) — single-domain,
minimal reach-in beyond `build_website_url`/CDP, a clean baseline website check to contrast against
069's complexity.

## 6. Provider-pattern risks

- **No `env.provider`/`env.manager` reach-ins** found anywhere in the 108 task files (`grep` for
  `env\.provider\b` / `env\.manager\b` / `path_to_vm` → zero hits) — task code stays behind the
  `DesktopEnv`/controller abstraction; no task reaches into provider internals directly. This is a
  clean signal for the E2B provider pattern.
- **`env.vm_ip` referenced directly** in 5 tasks (**022, 045, 074, 078, 082**): 022 and 045 build a
  CDP URL (`vm_ip:chromium_port`, i.e. port 9222 — already in the known set); 074 and 078 use it to
  target VM-internal command execution (still channel-relayed, not a new external port); 082 is the
  host-side `vm_ip:3000` case described in section 1 above — the one case needing genuine external
  port reachability beyond {5000, 9222, 8080}.
- **`prepare_volume`/`finalize_volume` disk sizing**: 18 of 108 tasks override `volume_size`
  (range 32–100GB). **Maximum requested: 100GB** (task **082**). 16 tasks override `instance_type`
  (AWS sizes `t3.large`/`t3.xlarge`/`t3.2xlarge`) — an AWS-provider-specific sizing hint with no
  direct E2B equivalent field; would need a mapping decision (ignore vs. translate to sandbox
  resource tier) but is not a functional blocker since no task reads `instance_type` back at
  runtime beyond passing it to the provider at VM-creation time.
- **No per-task guest-image overrides**: grepped for `image =` class-attribute overrides (the
  `BaseTask.image` field) across all 108 files — the only matches were unrelated local variables
  in evaluator code (e.g., `cv2.imread` results), not `BaseTask.image` overrides. All 108 tasks use
  the single pinned guest image; no task requests an alternate guest image.
- **VNC/recording opt-outs**: 1 task (**050**) sets `disable_vnc`/`disable_recording` — a supported,
  already-modeled runtime override (`DesktopEnv._apply_task_runtime_overrides`), not a surprise.
- **No host-filesystem-path assumptions** found (`os.environ['HOME']`/absolute host paths only
  appear in 2 files, both are guest-side path constructions inside VM-internal commands, not host
  filesystem reads by the harness process).
- **Reach-in histogram across all 108 tasks** (files matching each grep pattern; a task can match
  more than one): `:9222`(CDP) 56, `build_website_url` 33, `get_file` 10, `execute_python_command`
  9, `user_simulator` 7, `GITLAB_URL` 2, `:8000` 4 (all VM-internal), `:3000` 2 (1 VM-internal + 1
  host-direct), `:5000` 0 (never referenced literally — always reached via the controller objects,
  not a hardcoded port number in task code).
- **Domain diversity**: 46 distinct `related_apps` tags across 108 tasks (some tasks list 0, others
  up to 6). Most common: chrome (49 tasks), wps (13), vscode/vs_code (12 combined), mailhub (6),
  shotcut (6), gimp (5), zotero (5), libreoffice_writer (5) — a long tail of 30+ single- or
  double-occurrence app domains (freecad, kicad, 3d-slicer, blender, solvespace, geogebra,
  teamchat, and others named in the case doc's "expanded application set").
- **Setup/evaluate override rate**: 108/108 tasks (100%) define custom `setup`/`evaluate` — the
  declarative-config-only path in `DesktopEnv` is never exercised by this release's task set.
