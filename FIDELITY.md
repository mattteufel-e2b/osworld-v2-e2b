# Fidelity boundary — OSWorld 2.0 on E2B

Initial validation-ladder guest template: `osworld-v2-gnome:007fa754-617b-4ff0-957a-42ea4ecf5b8a`
(`out/osworld-v2-evidence/template-build.json`) — the hardening rebuild of `87d8a46b…`:
digest-pinned Ubuntu base image, sha256-pinned artifact downloads (VS Code .deb, Zotero,
REAPER, Compose v2 plugin), OpenBoard + snap-launcher compatibility, and the REAPER
startup-nag launcher. Earlier ladder receipts record the predecessor build each rung ran
against (`817519a3…`, `87d8a46b…`); each evidence file cites its own build id, and the
full-suite/focused receipts under `out/osworld-v2-evidence/full-suite/` cover the changed
surface on that build. The September 8 MiniMax campaign used
`osworld-v2-gnome:0d796343-1a70-4bd3-990e-8bb469b3dd20`; evidence for one build does not
certify another build. **Current build:
`osworld-v2-gnome:cd4a1a63-eb56-418f-9ac3-60fbbf2b838e`** serializes Linux AT-SPI reads and
adds Poppler for slide rendering. Its evidence covers the focused
concurrency and native-Claude checks below. The complete 108-task ladder belongs to
`00124a57…`; it does not certify this rebuild. Every claim in this ledger names its build.
Release pin: `osworld-v2-2026.08.08`. OSWorld-V2 checkout: `d578d2d4e0dc82b43e270fdaa7fa89d9708cd154`.

This ledger separates what was verified to match a stated reference, what was recorded
without a reference to match against, and what is excluded or unexercised outright.

## September 20 full native-agent Bedrock sample

[Campaign evidence](out/osworld-v2-evidence/native-parity-20260920/campaign.json)
records six tasks on immutable guest `cd4a1a63-eb56-418f-9ac3-60fbbf2b838e`,
using the pinned upstream Claude agent with Opus 5, adaptive max effort, up to
500 actions, zero action pause, and six workers. Live judges used Haiku 4.5.
Five tasks completed evaluation; task 079 stopped at the budget boundary.

| Task | Coverage | Actions | Score | Result |
| --- | --- | ---: | ---: | --- |
| 008 | Expense report and document attachments | 447 | 0.9091 | Evaluated |
| 030 | Training, checkpoint, and report | 124 | 1.0 | Evaluated |
| 041 | GitLab CI, artifacts, and Pages | 135 | 0.8215 | Evaluated |
| 079 | WPS presentation | 265 | — | Budget stop before evaluation |
| 087 | WPS presentation | 171 | 0.64 | Evaluated |
| 092 | Blender video and presentation | 151 | 1.0 | Evaluated |

All 22 attempted judge calls succeeded. Artifact audits verified task 008's three
DOCX attachments and 16 embedded images; task 030's fresh training outputs;
task 041's successful pipeline and deployed HTTPS page; task 087's 19-slide
PPTX and 19 fully decoded slide images; and task 092's Blender file, playable
five-second video, presentation, and fresh evaluator frame. Task 079 produced
modified 26-slide working decks but left the required output path unchanged.
Its artifacts were preserved read-only; no completion action or evaluation was
forced. The coordinator's receipt gate correctly failed because this task was
incomplete. These are partial task scores, not six successful task completions.

Inference accounting is **$116.0541**, based on provider-reported token usage,
cache TTL pricing, and a 10% allowance. This fresh allocation is separate from
previous samples and below the user's $125 limit. The final guard cap was $124;
its conservative $7.98 reservation for another full-context Opus call could not
fit, so further inference stopped. CountTokens preflight returned HTTP 403
because the key lacks `bedrock-mantle:CountTokens`. No charges remain unsettled.
Four guard replacements occurred with zero in-flight calls or request sockets;
recorded worker pauses total 106.883 seconds. This campaign does not establish
full-agent throughput. All campaign guests, service fleets, and local proxies
were stopped and verified absent.

The campaign exposed two native execution defects repaired in `runner/setup.sh`:
long `wait` actions exceeded the guest command deadline, and the `xclip` daemon
inherited captured pipes and prevented a completed typing command from returning.
The [wait control](out/osworld-v2-evidence/native-parity-20260920/wait-control.json)
completed a requested 240-second wait in 240.291 seconds. The
[clipboard control](out/osworld-v2-evidence/native-parity-20260920/native-clipboard-control.json)
reproduced the original action's HTTP 500 and verified HTTP 200 after the scoped
pipe fix, including preserved clipboard contents and ordinary command output.
These six rollouts began before the repairs; complete tasks were not rerun afterward.
GitLab runner registration, Pages routing, resource pagination, and cache-token
receipt accounting are also repaired and covered by targeted tests and controls.

Operational WPS and Blender coverage is established; exact reference parity is
not. There is no matched reference-VM run. The upstream launcher uses Sonnet 4.6,
which was absent from the tested Bedrock catalog; model, judge, and inline
checkpoint settings do not reproduce a published result. Bauhaus 93 is absent
and task 092 used Radis Sans. Blender 4.5.14 differs from the image-building
recipe's 5.0.0; the actual pinned reference AMI inventory remains unverified.
WPS symbol-font substitution remains documented. Native Unicode typing retains
upstream Ctrl+V behavior, which does not paste in the default GNOME Terminal.
Task 030's agent repaired dependencies in its task-created virtual environment;
this was not established as a template defect. Task 092's final presentation
links its video externally rather than bundling it. The complete 108-task ladder
and an 80-worker full-inference run remain unverified on this final guest.

## September 20 concurrency and native-Claude verification

[Evidence](out/osworld-v2-evidence/concurrency/verification-20260920.json) separates account
admission, desktop transport, task setup, and model execution. **Three workers is not a
measured ceiling.** The coordinator's default/cap remains 80; a separate bridge probe also
tested 98 workers. Strict reset can temporarily double guest count. An admission-only probe
held 201 guests simultaneously and deleted all 201; the account's upper quota remains unknown.

On `00124a57…`, the 98-worker probe observed two control-server deaths during accessibility
reads after screen-size and screenshot requests succeeded. Kernel/apport records showed
SIGSEGV in GLib with over 7 GB RAM available. A symbolized core enters `g_queue_push_tail`
through libatspi/DBus while another thread reads AT-SPI children. This supports the shared
native-state race; the first corrupting instruction was not identified. An 80-process repeat
of actual task-030 setup then passed 74/80; six exhausted accessibility retries with 502s.

`patches/osworld-server-atspi-serialization.patch` removes Linux per-application parallel
traversal and holds one shared lock around complete accessibility and terminal reads.
Windows/macOS behavior is unchanged. Rebuilt guest `be5ffc32…` passed task-030 setup and
observations in **80/80 independent processes**. Its **98-worker, three-reset** probe used
392 guest instances and returned HTTP 200 with nonempty trees throughout. These checks
exercise the reproduced failure; they are not a full 108-task evaluator or inference campaign.
Historical 502s without crash records cannot individually be assigned this cause.

Final build `cd4a1a63…` passed actual task-030 setup, screenshot, and nonempty accessibility
checks in **98/98 independent processes**, with no 502s. Eighty-five workers saw transient
accessibility HTTP 500 responses before bounded retries succeeded; maximum reset time was
73.05 seconds and maximum accessibility-readiness time 79.02 seconds. This establishes
completion under the tested cold-start load, not error-free first attempts.

The public pinned native Claude agent is available as `AGENT_KIND=claude`; Opus 5 is offered
by the tested Bedrock Mantle key. The live native-agent canary made eight calls, executed
eight native action dictionaries, and created a verified file on `be5ffc32…`. Mantle rejected
the upstream obsolete prompt-caching beta header; setup omits that header while retaining
cache controls, native prompts, tools, and parsing. A thin wrapper propagates failed
predictions, and beta Messages SDK usage is counted. See [baseline configuration and
comparison limits](docs/configuration.md#comparing-with-upstream-claude).

Native Opus 5 samples on `be5ffc32…` ran 30 steps each: Blender 092 opened Blender and
completed evaluation with score `0.2`; WPS 087 opened its presentation and returned `0.01`.
The WPS result exposed a missing `pdftoppm`: PDF export succeeded, but no slide PNGs were
produced. Evaluator completion alone did not prove functional slide rendering. Build
`cd4a1a63…` installs `poppler-utils` and requires its package and launcher in the build gate.
The direct 079 diagnostic omitted website-fleet wiring and failed before inference; it is
excluded from agent comparison.
The final-image [rendering control](out/osworld-v2-evidence/concurrency/render-control.json)
uses the actual evaluator export commands: 079 produced 26/26 valid slide PNGs and 087
produced 19/19. All 45 images fully decoded; the temporary guest was deleted. This control
uses unchanged inputs and verifies rendering, not task success or visual reference fidelity.

The final-image native Opus 5 sample for 087 completed 30 actions and evaluation in
378.4 seconds, downloaded and fully decoded all 19 slide PNGs, and completed 3/3 live
Haiku judge calls. Its score remained `0.01`; repaired rendering is not evidence of task
success. Blender and WPS sample scores use short budgets and do not reproduce a published
500-step result. All test guests and the local budget proxy were stopped.

Shared inference accounting totals **$125.05**: $59.72 estimated
measured usage plus $65.33 retained reservations for responses without usage.
This includes earlier samples, is below the $160 guard and $200 user limit, and is not an
AWS invoice. There are no calls still in flight.

An 80-way Opus 5 request burst returned 80/80 HTTP 200 responses in 2.58 seconds with
80 requests simultaneously reserved at the upstream boundary. It used tiny literal prompts,
32 output tokens, and disabled thinking, so it does not establish full-agent throughput.

## September 17 parity-build ladder, and the disposition of task 030

The guest template was rebuilt as `osworld-v2-gnome:c78db75e-7d61-4dea-a20b-25188f7aecdf` and
re-run through the full no-model ladder at 80 concurrent workers:
[summary](out/osworld-v2-evidence/full-suite/validate-108-parity-20260917.summary.json).
108 unique sandboxes, `external_model_calls: 0`, checkout `d578d2d`: **106 `PATH_PASS`, one
`MODEL_BOUNDARY_PASS` (035, the judge boundary reached and refused as designed), and one
`PATH_FAIL` (030)**. The previous ladder on `0d796343…`
([summary](out/osworld-v2-evidence/full-suite/validate-108-bridge-20260914.summary.json))
recorded 107 `PATH_PASS` and zero failures, so the committed file reads as a regression. It is
committed red and stays red; this is its disposition, not a correction of it.

That ladder is evidence for `c78db75e…` and for no other build. The template has since been
rebuilt as `osworld-v2-gnome:00124a57-267c-45e7-93d1-ca0ff196e4b8`, which drops the
`/execute` timeout patch and slims the baked first-run configs to their decisive keys. That
build carries the application-launcher smoke
([receipt](out/osworld-v2-evidence/template/app-smoke-00124a57-267c-45e7-93d1-ca0ff196e4b8.json))
and now its own ladder, run on the integrated branch — see the next section.

## September 18 final-build ladder and live verification

`osworld-v2-gnome:00124a57-267c-45e7-93d1-ca0ff196e4b8` was taken through the full no-model
ladder at 80 concurrent workers on checkout `d578d2d`:
[summary](out/osworld-v2-evidence/full-suite/validate-108-final-20260918.summary.json).
108 unique sandboxes, all unique, `external_model_calls: 0`, host proxy owned for the
campaign: **104 `PATH_PASS`, one `MODEL_BOUNDARY_PASS` (035, the judge boundary reached and
refused as designed), and three `PATH_FAIL` (008, 009, 030)**. The gate reports **FAIL** on
that receipt and the summary is committed red.

These three share a reset/observation failure signature. Each records `stage: reset`, `cause:
reset-or-observation`, `evaluator_ran: false`, `score: null`, with a second-generation guest
(post strict-reset) answering `502` to every accessibility-tree request; 008 and 009 also drew
`500` from the CDP `/json/version` endpoint. All three started inside the first three seconds
of the 80-worker launch wave. The 5xx responses are in the gitignored worker logs
(`out/osworld-v2-raw/live-osworld-live-20260918T000607Z/ladder/no-model-raw/workers/task_{008,009,030}.log`).
The launch timings are not: neither those logs nor the coordinator's own log
(`out/osworld-v2-raw/live-osworld-live-20260918T000607Z/ladder/coordinator-stdout.log`) carries
a timestamp — the coordinator log only prints `launched task NNN pid=…` lines in order. The
timings come from the gitignored aggregate receipt
(`out/osworld-v2-raw/live-osworld-live-20260918T000607Z/ladder/no-model.json`), whose per-record
`started_at` values — `2026-09-18T01:04:38.836599+00:00` (008), `01:04:40.084920` (009), and
`01:04:41.363405` (030), against the run's first launch at `01:04:38.429432` — place all three
in that window. The committed summary carries no per-task `started_at`, so the raw receipt is
the only source for the timings.
This is the reset-time ingress 5xx burst already recorded for
030 on `c78db75e…` and for 043 on `0d796343…`. A follow-up wave re-ran exactly those three on
fresh sandboxes at concurrency 3 and all three passed:
[summary](out/osworld-v2-evidence/full-suite/validate-108-final-followup-20260918.summary.json).
Read together, all 108 tasks reached the harness's passing boundaries on this build — the same
disposition the September 11 ladder gave 043 and 082. Those low-concurrency retries did not establish high-concurrency reliability. The failure
rate was 3 of 108; the September 20 investigation above reproduces a native control-server
crash and tests its fix at high concurrency.

Two live controls ran against the same build and the same fleet campaign. The guest-browser
secure-context probe
([summary](out/osworld-v2-evidence/fleet/browser-probe-00124a57-267c-45e7-93d1-ca0ff196e4b8.summary.json),
`maintainer/browser_probe.py`) drove the guest's own Chrome 153 over CDP across TeamChat,
CloudCRM, MailHub, StreamView, `studio.streamview` and the task-041 GitLab alias. The
long-typing control
([result](out/osworld-v2-evidence/controls/typing-control-00124a57-267c-45e7-93d1-ca0ff196e4b8.json))
is covered in the controller-patch section below.

Nine application and fleet tasks then ran with the upstream M3 agent at 500 steps, concurrency
9, Haiku 4.5 on Bedrock Mantle configured as judge, and the same model configured as user
simulator with no provider or transport recorded in the receipt:
[summary](out/osworld-v2-evidence/live/agent-nine-20260918.summary.json). The summary carries
no judge or simulator configuration fields — `maintainer/receipt_summary.py` keeps none — so
that configuration is checkable only in the gitignored raw receipt
(`out/osworld-v2-raw/live-osworld-live-20260918T000607Z/inference/agent-nine.json`, its
`evaluator` and `user_simulator` blocks), and so is the gate verdict below
(`summary.invalid_task_ids` in the same file). Nine unique
sandboxes, all unique, `retried_task_ids: []`, `implicit_retries: false`. Eight of nine
receipts are `path_status: OK` with an evaluator that ran; the ninth (035) is `ERROR` and the
gate reports **FAIL**, as it should. Scores, recorded verbatim and not asserted: 092 `0.2`,
079 `0.05`, 087 `0.01`, and `0.0` for 026, 038, 041, 067 and 107 — mean partial `0.0325`,
binary accuracy `0.0`. The September 20 investigation found `pdftoppm` absent from this
build: 079/087 slide-rendering scores are affected by that environment defect and cannot
be treated as agent-performance or parity evidence. The recorded scores remain unchanged.

035's `ERROR` is the agent, not the infrastructure: at step ~406 it typed `killall -9 python3`
into a guest terminal (its gitignored trajectory,
`out/osworld-v2-raw/live-osworld-live-20260918T000607Z/inference/agent-raw/workers/task_035/runtime.log`
line 4105, records the action verbatim), killing the guest's own OSWorld control server, after
which every `/execute` and screenshot returned `502` and the harness failed closed with no
evaluator call. Upstream-unmodified agent behaviour; no prompt, timing or scoring change was
made to avoid it, and the receipt is recorded rather than retried.

The run's own token counts give two different cost figures, and a customer reconciling against
the cited summary will get the smaller one. All nine records together are 4,382 agent calls,
24,855,448 input and 628,610 output tokens, which at Fireworks MiniMax-M3 list price ($0.30 per
million input, $1.20 per million output) is **$8.21** — the cost of the whole run, mean **$0.91
per task** across the nine. The summary's `model_usage` is not that: `runner/aggregate_agent.py`
sums attested records only, so the committed summary reports the eight attested receipts —
3,975 calls, 23,005,009 input, 575,693 output, **$7.59** — and 035's own 407 calls
(1,850,439 input, 52,917 output, $0.62) are absent from it because its receipt did not pass the
gate. Both are list-price figures derived from the receipts, not billed amounts
(`out/osworld-v2-raw/spend-ledger.json`). No receipt, summary or config in this repo records
the rates; they are the provider's published list prices, stated here so the arithmetic can be
checked against the token counts rather than taken on trust.

Judge and simulator calls were **zero** — none of these nine evaluators invoked a model. The
judge configuration itself was proven live by the coordinator's preflight probe
(`out/osworld-v2-raw/live-osworld-live-20260918T000607Z/inference/coordinator-stdout.log`
line 3, `live model check ok: text + image judge, 1 simulator configuration(s)`), so that `$0`
is an absence of calls, not an absence of capability.

Task 030's record is `stage: reset`, `evaluator_ran: false`, `score: null` — the reset never
completed, so nothing was scored and no evaluator ran. Its gitignored worker log
(`out/osworld-v2-raw/parity-ladder/no-model-raw/workers/task_030.log`) contains twelve failed
accessibility-tree fetches: nine `Failed to get accessibility tree. Status code: 502`
responses in three groups of three, each group followed by the controller's bounded-retry
exhaustion line (`get_accessibility_tree` retries `retry_times = 3`), after which the harness
failed closed at 117.2 s. Five sibling workers in the same wave logged the same getter failing
and recovered inside or after one retry round — 004 (four occurrences, one exhausted round),
008 (one), 014 (two), 027 (one), 091 (one) — all with status 500 rather than 502; only 030
exhausted the budget repeatedly. An isolated re-run of 030 alone at `PARALLEL_CONCURRENCY=1`
on the same build reached `stage: complete` with `PATH_PASS`, `evaluator_ran: true` and no 502
at all; that re-run is recorded separately and was deliberately **not** folded into the
committed summary. Task 030's own `related_apps` are `vscode` and `terminal` — neither is an
application this template change added or reconfigured.

These historical receipts do not distinguish ingress failure from a dead guest control
server. The September 20 investigation above observed native AT-SPI/GLib crashes with the
same HTTP symptom and verified the serialization fix against an 80-process task-030 repeat.
The historical failures lack cores, so their individual causes remain unproven.

## September 14 judge and 36-task full-inference verification

The [36-task campaign](out/osworld-v2-evidence/sample-36/agent-main36-m3-500-20260914.json)
used the unchanged upstream M3 agent on Fireworks with a 500-turn cap, 8,192 output tokens,
2,048 thinking tokens, an eight-hour task deadline, and no task retries or prediction
resampling. The native SDK's transport retries remain unchanged. Guest `0d796343…`, fleet
`76176a74…`, and checkout `d578d2d` identify the exact inputs; the
[manifest](out/osworld-v2-evidence/sample-36/manifest-36.json) and
[settings](out/osworld-v2-evidence/sample-36/run-settings-20260914.json) record
the configuration. This is targeted coverage, not a random sample or a 108-task model result.

Thirty tasks exhausted the cap, four ended through native agent termination, and two failed
without scores. **34 scores exactly match upstream result files; the campaign gate is FAIL**
for 003 and 048. Task 003 killed its user-owned Python desktop control server through an agent
terminal action and then failed while writing a missing screenshot. Task 048 exhausted five
native 600-second setup-upload attempts on the Mac coordinator before any inference.

A [separate Linux task-048 run](out/osworld-v2-evidence/sample-36/agent-linux-048-m3-500-20260914.json)
installed the locked root and full upstream environments from scratch, transferred the exact
274,509,060-byte pinned archive through the native E2B files API, and exhausted its 500-turn
budget (499 actions and one ASK_USER turn). Its native score is 0.0 and its receipt gate is
PASS. The downloaded raw archive passed SHA256 verification before runner deletion. This
closes that task's cloud execution path; the original Mac failure remains in its own campaign.
The Linux source bundle matches the final judge probe, tracker, preflight, and coordinator
byte for byte. Fresh guest restores also match the patched server routes, APT wrapper, Chrome
shim, and server requirements file, including the installed AnyIO 4.14.2 pin. These focused
checks do not constitute a complete image inventory or a fresh template build.

Judge and LLM simulator calls used `anthropic.claude-haiku-4-5` through AWS Mantle. Correct
upstream simulator environment names are documented, ignored aliases are rejected, and a live
text/image/simulator probe runs before rollout admission. Empty model results cannot count as
successful calls. **43 known-answer judge controls and all six task-specific LLM simulator
controls passed**: tiny budgets, spreadsheet positive/negative/partial-credit cases, single-
and multiple-verdict visual checks, and structured/semantic text cases. The
[control evidence](out/osworld-v2-evidence/sample-36/judge-controls-20260913.json) is separate
from agent scores. Fireworks M3 awarded full credit to four of five blank-slide negative
controls with reasoning disabled, so successful image transport does not qualify it as a
judge. Haiku control results do not establish parity with the published benchmark's judge.

The [main artifact audit](out/osworld-v2-evidence/sample-36/audit-main36-20260914.json)
contains 16,588 trajectory entries, 16,565 action steps, 23 ASK_USER turns with nonempty replies,
and 16,565 valid 1920×1080 PNGs. The additional empty PNG is the known task-003 failure;
no referenced screenshot is missing. Nine real judge calls and three real LLM simulator
calls succeeded. All 16,589 returned M3 responses were nonempty, with no logged inference
exceptions. These are predict-level counts, not billed requests or token usage.

Desktop-command success is a separate boundary: 25 tasks logged 350 native command-failure
messages. In an isolated control, upstream M3's 1,000-character action generated 1,000 keypress
calls with the guest's 0.1-second PyAutoGUI pause. The unchanged upstream controller returned
`None` after 90.12 seconds while typing continued; completion appeared by 102.61 seconds.
The [runtime controls](out/osworld-v2-evidence/sample-36/runtime-controls-20260914.json)
record that failure mode and preserve it separately from inference success. No action timing,
agent prompt, task, or scoring-function changes were made to improve these results.

The same controls and actual setup logs established four fidelity gaps, every one of which was
re-tested on the September 18 final-build run above and is now closed as a *setup* condition —
which is not the same as a score. The evidence for each is gitignored, under the campaign
directory `out/osworld-v2-raw/live-osworld-live-20260918T000607Z/`: the per-task setup logs are
`inference/agent-raw/workers/task_<id>.log` and the trajectories are
`inference/agent-raw/workers/task_<id>/runtime.log`.

- **Applications.** The five tasks whose applications were missing or incompatible (067
  `musescore`, 079 and 087 `wpp`, 092 `blender`, 107 `kicad` failing with APT code 100 behind a
  FreeCAD package hold) show no launcher or install failures in the worker logs on build
  `00124a57…`: zero occurrences of `command not found`, zero of `Failed to launch application`,
  and for 107 zero `apt-get` activity, with the agent opening KiCad in its first steps. That
  closes launcher presence and setup success, and nothing more. The original 067 finding
  (`docs/pr-1-verification.md:114`) has a second half — installed `musescore3` is 3.2.3 and
  rejects the input created by MuseScore
  4.6.5, producing no export — and that half is untested and still open: `musescore3` is still
  3.2.3 on this build (recorded under "Unpinnable applications" below), nothing in this wave
  opened or exported the pinned score, and 067 scored `0.0`. It does not establish MuseScore
  file compatibility.
- **HTTP fleet origins.** Now HTTPS under a per-campaign CA, and verified from inside the
  guest's own Chrome rather than inferred — see the browser probe below.
- **Task 026's stale active asset URL.** The state the run actually loaded
  (`026-seeded-state-thisrun.json`) carried only
  `https://files.127.0.0.1.nip.io/task_026/AI-Assisted_Healthcare.zip`, with no
  `huggingface.co` reference anywhere; the in-guest fetch (`026-asset-in-guest.json`) returned
  `200`, 3,176,733 bytes and the pinned sha256, and the `HEAD` reported the matching
  `Content-Length`.
- **Task 041's hardcoded GitLab URL.** The run's first Chrome tab
  (`inference/agent-raw/workers/task_041/step_1_*.png`) is our own deployment's
  "Sign in · GitLab" through the alias, with no certificate interstitial.

Successful native score reporting still does not certify those starting conditions, and a
closed setup gap is not a task pass: eight of the nine tasks were scored and every one of those
eight scored at or near zero, while the ninth (035) produced no score at all. Task 069 reached
only phase 1 before its native gate stopped progression. Native media controls exercise XCF
extraction, video decoding, MuseScore XML scoring, and REAPER scoring; the focused REAPER
fixtures exclude safety-baseline setup. They do not establish GUI file compatibility or
audio-rendering parity.

[Cleanup verification](out/osworld-v2-evidence/sample-36/cleanup-20260914.json) found zero
remaining campaign sandboxes across API-listed states and all 146 checked local ports free;
service runtime and token files were removed. The Linux runner and isolated diagnostic guests
were also deleted. The [review](out/osworld-v2-evidence/sample-36/review-20260914.md) separates
PR findings from pre-existing fidelity blockers. These results support the judge fix and
specific runtime paths, not customer readiness or QEMU/E2B benchmark parity.

## September 11 release validation and recording check (runner split)

After the runner split (`runner/` = benchmark path, `maintainer/` = no-model ladder,
`tools/spikes/` = one-off probes, one shared `runner/worker_lib.sh` for relay/watchdog/teardown)
and the `ENABLE_RECORDING` opt-in, the ladder and an agent rollout were re-run on template
`0d796343…`, checkout `d578d2d`.

**No-model ladder, all 108 tasks, 80 concurrent workers** (the coordinator's cap; strict reset
can briefly double guest count): [receipt](out/osworld-v2-evidence/full-suite/validate-108-20260911.json).
107 receipts, 107 unique sandboxes, zero external model calls: 105 `PATH_PASS`, one
`MODEL_BOUNDARY_PASS` (035, the judge boundary reached and refused as designed), one `PATH_FAIL`
(043: the receipt records `cause: environment/reset`; the gitignored worker log shows the
second-generation guest answering 502 to every accessibility-tree request for ~87 s before the
harness failed closed), and 082 with no
receipt because its worker preflight found host port 3000 taken by an unrelated local dev
server. Task 082's upstream task file hardcodes `localhost:3000`, so its relay listener cannot be
shifted like every other port. The aggregate gate reports **FAIL** on that receipt, as it should.

A follow-up wave re-ran 043 and 082 on fresh sandboxes with the port freed:
[receipt](out/osworld-v2-evidence/full-suite/validate-043-082-followup-20260911.json). Both
`PATH_PASS` (043 in 33 s, 082 in 84 s), gate **PASS**. Read together, all 108 tasks reached
the harness's passing boundaries. Those checks did not establish application-launch or
file-format compatibility: the [September 14 runtime controls](out/osworld-v2-evidence/sample-36/runtime-controls-20260914.json)
confirm missing WPS/Blender/MuseScore launchers, an incompatible MuseScore version, and a
package hold blocking KiCad installation on this build. The 043 failure did not reproduce
and has no crash record establishing its cause; see the September 20 investigation of this
HTTP symptom on the application-complete build.

**Agent rollout with recording** (task 093, upstream M3 agent, `accounts/fireworks/models/minimax-m3`,
500 steps, 2,048 thinking tokens, judge pointed at Fireworks MiniMax because the OpenAI key has
no credits; task 093 has no user simulator, and simulator overrides are unset in the receipt): [receipt](out/osworld-v2-evidence/sample-24/agent-093-recording-20260911.json).
Path OK, 500 steps, score 0.25 matching upstream `result.txt`, no judge or simulator calls, gate **PASS**. The receipt records `recording_enabled: true` and the guest's ffmpeg capture landed as a 67 MB `recording.mp4` next to the trajectory in the raw result directory. Recording is therefore verified end to end and moves out of the excluded list below; it stays off unless a run opts in. All campaign sandboxes and both fleets were cleaned up.

Unchanged from the September 10 entry: this is runtime verification on E2B, not a parity claim
against the paper's 108-task, Claude-Sonnet-judged numbers.

## September 10 sample validation (agent construction decoupled)

The [13-task receipt](out/osworld-v2-evidence/sample-24/agent-live13-m3-500-20260910.json)
re-ran the September 8 sample after agent construction moved into `runner/agents.py`
(`AGENT_KIND` registry, upstream generation defaults, `--max-tokens/--temperature/--top-p/
--max-trajectory-length` flags). Same model (`accounts/fireworks/models/minimax-m3`, upstream
M3 agent, 500 steps, 2,048 thinking tokens, `M3_MAX_LLM_RETRIES=2`, no task retries), same
guest template `0d796343…`. Every receipt records the resolved `agent_settings`
(8192 tokens, temperature 0.6, trajectory 10, screenshot/pyautogui), i.e. the exact values
the runner previously hardcoded. Judge and simulator used upstream's per-task model defaults
with the OpenAI provider rather than the September 8 MiniMax overrides. 13 concurrent workers were requested;
the campaign ran as 7 + 6 because the local host, not E2B, was memory-constrained.

Twelve tasks completed evaluation on unique sandboxes and every recorded score exactly matches
its upstream `result.txt`: 093 scored 0.86, 083 0.22, 103 0.10, and eight tasks scored zero
(003, 015, 019, 026, 048, 053, 059, 105). Tasks 053 and 103, unscored errors on September 8,
evaluated cleanly. Two tasks are invalid for one shared reason: the OpenAI judge key had no
credits (`insufficient_quota`), so 035's whole-table judgment raised and 079's five VLM calls
failed (upstream turned that into 0.01; the receipt gate refuses to attest it, as designed).
The gate reports **FAIL** on those two, not a passing 13/13. Mean over the eleven attested
tasks is 0.107 (September 8: 0.093 over ten); binary successes 0 in both.

A separate campaign re-ran 035 and 079 with the judge and simulator pointed at Fireworks
MiniMax (`openai_compatible`, the September 8 override):
[receipt](out/osworld-v2-evidence/sample-24/agent-judge2-m3-500-20260910.json). Both tasks evaluated on unique sandboxes with 6/6 judge calls succeeding (035: one call, 480 steps; 079: five calls, 500 steps); both scored 0.0 and the gate reports **PASS**. 035 did not reproduce the September 8 JSON-parse failure with the same MiniMax judge; one clean run does not establish that failure is gone, so that item stays open as intermittent.
Because its judge differs, that receipt is reported alongside the 13-task receipt, not merged into it.

Public reference for the same model: the OSWorld 2.0 paper (arXiv 2606.29537, Table 3) reports
MiniMax M3 at 4.6% binary / 22.3% partial over all 108 tasks at 500 steps with Claude Sonnet 4.6
as judge on AWS. The E2B sample is 13 tasks, one seed, a different judge, and a Fireworks-hosted
model, so it is a plausibility check (same order of magnitude, zero binary successes, partial
credit on the same kinds of tasks), not a parity measurement. Per-task public results live in
the gated `xlangai/osworld2.0-trajectory` dataset, which this account cannot read.

An earlier attempt of the same campaign on September 9 lost all guests at once (relay DNS
failures, then `Sandbox not found`) while the host laptop slept; its 11 errors carry the
identical screenshot-502 signature and are recorded only as an infrastructure note, not as
results. The rerun was wrapped in `caffeinate`.

## September 8 parallel sample validation

The [13-task receipt](out/osworld-v2-evidence/sample-24/agent-final13-m3-500-20260908.json)
records a completed campaign with 13 concurrent workers and unique guest sandboxes,
using `accounts/fireworks/models/minimax-m3`, the upstream M3 agent, a 500-step budget,
2,048 thinking tokens, and no task retries. Judge and simulator settings explicitly
overrode upstream defaults with the same MiniMax model; this is not a native-default
model comparison. The campaign ran the reviewed runtime changes on top of `4506c1e`.

Ten tasks completed evaluation; every recorded score exactly matched its upstream
`result.txt`. Tasks 083, 093, and 105 scored 0.10, 0.63, and 0.20 respectively; the other
seven evaluated tasks scored zero. Tasks 035, 053, and 103 remain unscored errors, not
zero-score substitutions. The aggregate receipt gate correctly **fails**. Its mean is
over the ten scored tasks only and is not a complete-sample benchmark result. All owned
campaign sandboxes were cleaned up.

The remaining investigations are separate from accepting the runtime integration:

- **035 — judge response validity:** the model call returned, but the upstream evaluator
  could not parse its JSON. Response truncation is plausible, not established. Diagnose
  with upstream raw-response/debug logging; do not repair JSON or alter scores in the port.
- **053 — upstream M3 action parser:** malformed coordinates raise `IndexError` before
  action execution. Coordinate validation belongs upstream, not in an E2B-specific parser.
- **103 — long-action/control failure:** screenshot acquisition failed after 293 steps.
  Long typing actions can outlive upstream execution timeouts; the final observation
  failure still needs a matched native/E2B investigation. An E2B contribution is not ruled
  out, and earlier successful transport does not certify the failed observation.

This supports merging a runtime integration with explicit limitations, not certifying
full native-result parity or a passing 24-task sample.

## Matched

- **Guest OS**: Ubuntu 22.04, GNOME Shell 42.9 — ported unchanged from hark's proven
  OSWorld 1.0 desktop reconstruction (`out/osworld-v2-evidence/live-smoke.json`, `versions`).
- **Source-pinned applications** (deterministic, versioned artifact, not apt-latest):
  `code` 1.91.1-1720564633 (Microsoft's permanent versioned `.deb`), `zotero` 7.0.15
  (versioned tarball from zotero.org), `reaper` 7.79 (versioned tarball from reaper.fm).
  `out/osworld-v2-evidence/template-build.json` `pinnedVersions`.
- **Guest control server**: V2's FastAPI/uvicorn `osworld-server`, vendored at
  `xlang-ai/osworld-server@a3cc3f0c64e463f020d1a44780307e9b46cbcab1`, with hark's null-safe
  AT-SPI serializer guards re-applied, plus the exact-process-path open-file fallback and
  near-silent ffmpeg recording flags. The unlicensed source is generated only by
  `template/fetch_server.sh`; the repository carries three reviewable patches against the pin
  (`patches/osworld-server-atspi-guards.patch`, `patches/osworld-server-runtime-reliability.patch`,
  `patches/osworld-server-atspi-serialization.patch`),
  not a copy of upstream source. V2 carries the identical AT-SPI defect — `role`, `name`,
  action description, key binding, stripped role-name — at the same call sites as V1.
  Corroborated live: accessibility observations returned substantial, non-empty content
  with no serialization crash across every one of the 20 validated sandboxes
  (`out/osworld-v2-evidence/validate-run1.json`, `validate-run2.json`,
  `accessibility_characters` present and nonzero on every record).
- **Provider contract**: `E2BVMManager`/`E2BProvider` registered by `runner/setup.sh`, whose
  footprint is declared and verified by `runner/setup.sh`: patches (a)–(n) cover provider
  registration, strict reset, execution and agent transport compatibility, plus vendored
  provider files (`provider.py`, `manager.py`, `bridge.py`, `e2b_policy.py`). Patch
  anchors and logic are committed at `runner/setup.sh`; `setup.sh --verify` proves exactly
  this set before a run and `--restore` removes it.
  Verified working, not just applied: the environment-path validation ladder created 20
  unique sandboxes through this exact provider registration, all `PATH_PASS`
  (`out/osworld-v2-evidence/validate-run1.json`, `validate-run2.json`). Destroy-and-
  recreate revert semantics transfer from V1 unchanged.
- **Snapshot semantics**: live-probed, not inferred — a marker written before `save_state`
  survives a revert into a new sandbox; an unsaved name falls back to the template's neutral
  state (marker absent); restricted ingress is preserved across both paths. 12/12 checks,
  `out/osworld-v2-evidence/snapshot-probe.json`, `all_checks_passed: true`.
- **Restricted ingress + traffic-token auth**: unauthenticated requests to the guest port
  403, authenticated 200 — verified at the guest control port and again at the service
  fanout ports (`out/osworld-v2-evidence/live-smoke.json`, `out/osworld-v2-evidence/spike-ingress.json`).
- **Sandbox network boundary**: every guest and service sandbox uses secure envd and
  authenticated ingress. Public egress remains available for upstream setup downloads while
  RFC1918, link-local/metadata, carrier-grade NAT, benchmark, multicast, and reserved destination
  ranges are denied. The shared Python policy is unit-tested, and the exact guest build was
  restored successfully with the same policy during its post-build smoke.
- **Root-volume contract**: OSWorld `volume_size` is mapped to minimum usable root-filesystem
  capacity, not to an E2B persistent Volume. All 18 requesting tasks use 32–100 GB; the exact
  immutable guest restore reported 103,495,585,792 usable bytes and passed the 100 GB release
  maximum. `prepare_volume` records the request and `finalize_volume` fails before task setup if
  the live root is undersized. See `validation/volume-requirements.json` and
  `out/osworld-v2-evidence/template-build.json`.
- **Boot-time credential**: verified as PAM verifies it, not merely "chpasswd ran" —
  `crypt(candidate, stored_salt) == stored_hash` read from `/etc/shadow` via the guest's
  own NOPASSWD sudo channel; correct password matches, wrong password does not
  (`live-smoke.json` `su_auth`).
- **Audio path**: PulseAudio null sink `vsink` present, a rendered tone plays to the sink
  successfully (`out/osworld-v2-evidence/spike-audio.json`). REAPER and MuseScore are baked
  to their Dummy/PulseAudio backends, not a hardware ALSA path — see Recorded-only for the
  kernel-module boundary this implies.
- **First-run modal absence, showing-gated**: every V2-added first-run-prone app (MuseScore,
  FreeCAD, REAPER) plus the carried-over V1 set (Chrome, LibreOffice, VLC) shows its main
  window present AND its first-run modal absent, using an AT-SPI `showing`/`visible` flag
  check that is strictly stronger than V1's presence-only check (a suppressed-but-present
  dialog object no longer trips a false positive) — `live-smoke.json`, Task 10 report "Showing-
  gated modal detection".
- **Environment-path completion**: two independent 10-task passes, 10/10 `PATH_PASS` each,
  20/20 unique sandboxes, every evaluator ran without an infrastructure error
  (`out/osworld-v2-evidence/validate-run1.json`, `validate-run2.json`).
- **Full-agent transport**: 10/10 rollouts transport-clean (0 transport errors, 0 evaluator
  crashes) across parallel namespaced relays (`out/osworld-v2-evidence/agent-run-summary.json`
  `gate`).

## Recorded-only (no reference to certify against, or best-effort under load)

- **Unpinnable applications** — apt-mark held so the immutable build freezes whatever
  version was served at build time, but not matched to any V2 reference image (V2, like
  V1, has no published reference-image version inventory to check against):
  `google-chrome-stable` 152.0.7977.64-1 (Google's apt repo serves only latest stable, no
  archived `.deb`), `musescore3` 3.2.3+dfsg2-11, `shotcut` 22.01.30+ds-1, `freecad`
  0.19.2+dfsg1-3ubuntu1 (all single-version Ubuntu 22.04 universe packages, no archived
  `.deb` URL). Deterministic-per-release but not source-pinned: `libreoffice` 7.3.7.2,
  `gimp` 2.10.30, `vlc` 3.0.16, `thunderbird` 140.7.1esr, `evince` 42.3, `pulseaudio`
  15.99.1 (jammy pockets). Full table: `out/osworld-v2-evidence/template-build.json`
  `unpinnableVersions` + `unpinnableVersionsNote`.
- **Task-service port mapping**: `OSWORLD_TASK_SERVICE_PORTS` accepts either a literal `port`
  entry for backward compatibility or an explicit `local:guest` mapping. `task_082`, the sole
  task that host-dials a guest service, uses its canonical host port 3000 mapped to guest port
  3000. Preflight rejects a run containing `task_082` when host port 3000 is unavailable; because
  no other release task owns that listener, it can otherwise run in the concurrent wave without
  routing into another worker's guest.
- **Evaluator-model transport**: the pinned evaluator backend retains upstream timeouts and
  retry behavior. Judge and simulator overrides are opt-in and recorded separately from agent
  configuration. Failed model calls invalidate a run without rewriting upstream scores or
  retrying scored attempts. Simulator calls cannot satisfy judge coverage. Runners use real OCR
  and audio evaluator dependencies through upstream's `full` extra, without dependency stubs;
  easyocr is imported lazily so torch loads only in a worker whose task runs an OCR metric.
- **Per-task wall clock**: `AGENT_TASK_TIMEOUT_SECONDS` (default 14400 s) bounds a
  task rollout; upstream has no per-task deadline. A timed-out task produces a
  fail-closed `task-timeout` receipt (never a score) and is eligible for the
  infrastructure retry wave — unless a completed receipt already exists when the
  deadline fires, in which case that scored receipt is kept.
- **Setup upload paths**: the bridge serves `POST /setup/upload` directly through
  the E2B files API and requires absolute guest paths; upstream's in-guest server
  also accepts `~`- and `$VAR`-relative paths. All 108 release tasks use absolute
  paths; custom configs with relative upload destinations must be rewritten.
- **Guest apt front-end**: the template installs a `/usr/local/sbin/apt-get`
  wrapper forcing `DEBIAN_FRONTEND=noninteractive` and conffile-keep defaults so
  task-driven package operations cannot hang a rollout; a stock VM would prompt.
- **Audio kernel path**: no `snd-dummy`/`snd-aloop` ALSA kernel module in the Firecracker
  guest kernel (`modprobe: FATAL: Module snd-dummy not found`, `spike-audio.json`). The
  PulseAudio null-sink path was sufficient for every app exercised (REAPER and MuseScore
  both configured to non-ALSA backends), so this did not block any verified check, but a
  task requiring a real ALSA device would hit this boundary.
- **Fonts, seeded profiles, application preferences/accounts**: not reproduced or certified
  against any reference image, same caveat as the V1 conversion — no file-by-file reference
  inventory exists to check against (V2's reference is a gated qcow2/AMI, never inspected).
- **Task 092 font and Blender version**: the task requires Bauhaus 93, which is absent
  from the E2B guest and is not supplied by the task setup or local task assets. Its evaluator
  does not explicitly verify that font, so a high score cannot certify font fidelity.
  The [pre-August upstream image recipe](https://github.com/xlang-ai/osworld_image/blob/d0ad458c0655bddea4e628437019f153718de947/ansible/group_vars/all.yml#L104)
  specifies Blender 5.0.0; this guest pins 4.5.14 LTS. That is a recipe difference, not an
  inventory of the pinned reference AMI. Neither the reference VM's Blender version nor its
  Bauhaus 93 availability has been verified.
- **Symbol and Wingdings are absent from the guest; only WPS's warning about them is
  suppressed**: a fresh `wpp` raises a "System Check" window reading "Some formula symbols
  might not be displayed correctly due to missing fonts Symbol, Wingdings...". The baked
  `wps-office.conf` carries the key WPS itself wrote when that window was dismissed
  (`common\system_check\no_necessary_symbol_fonts=false`), so the window no longer appears —
  but the fonts are still not installed, and a document using them renders with a substitute
  face. Tasks 049/060/077/079/087/090/096 drive `wpp`, so substituted glyphs could change
  rendered output and therefore a score. The open fonts the template does install alongside WPS
  (Carlito, Caladea) do not supply either face.
- **Service fleet topology**: mocked websites and GitLab are self-hosted-in-sandbox per
  campaign (fresh fleet per campaign), not a team-hosted shared deployment — a deliberate
  choice (turns GitLab's shared-root-token problem into per-run isolation; deferred
  alternative noted, not implemented). Each site's state is cookie-scoped via its own
  `/api/state`; the September 14 audit found no cross-task cookie collisions across 22
  standard website sessions. Since the September 15 fleet wave the origins are **HTTPS**,
  terminated by the guest proxy on 443/8090 under a CA minted per campaign and installed into
  both the guest's system store and Chrome's own NSS database, so the guest sees secure
  contexts rather than the HTTP origins that previously disabled notifications and the
  clipboard API. On build `00124a57-267c-45e7-93d1-ca0ff196e4b8`, the
  [live controls](out/osworld-v2-evidence/pr5-bedrock-sample/controls.json) pass all
  56 browser-probe checks. TeamChat, CloudCRM, MailHub, StreamView and
  `studio.streamview` are tested on the task port **8090**; task 041's hardcoded
  GitLab alias is tested on 443. The probe verifies secure contexts, browser APIs,
  notification grants, clipboard round-trip, cross-site cookie isolation, and
  secure subresource requests. A 2 MiB StreamView upload is retrieved byte-for-byte,
  and a 2 MiB chunked Git push from the guest matches the blob recorded by GitLab.
  Temporary upload state and the GitLab project are removed afterward.

  These checks cover the probed pages and two write paths, not every application
  interaction. The upload is a transport payload, not a video-playback test. The topology still
  differs from an official hosted deployment. The GitLab page issues
  `https://gitlab.127.0.0.1.nip.io:80/-/collect_events` and gets
  `net::ERR_SSL_PROTOCOL_ERROR` — an `https` scheme against the port the guest proxy serves in
  plain HTTP, traced to `gitlab.external_url` (`http://`) disagreeing with `gitlab.url`
  (`https://…:8090`) in the campaign runtime file. CDP confirms it as a failed handshake and
  **not** a mixed-content block (`blockedReason: null`). It is Snowplow telemetry, the page
  renders and signs in, and no task in the 108 asserts on GitLab analytics — so it is disclosed
  and left unfixed here, not repaired quietly. Finally, these applications fetch live public
  CDNs (`picsum.photos`, `fonts.googleapis.com`, `fonts.gstatic.com`, `img.youtube.com`), all
  over HTTPS, so their pages depend on public egress at run time and would degrade on a
  network-restricted host for reasons unrelated to any agent action.
  `out/osworld-v2-evidence/services-websites.json`, `services-gitlab.json`.
- **Ingress addressing mode**: per-port fanout + host+guest Host-mapping proxies, not
  single-port Caddy Host-header passthrough. Directly probed and ruled out: E2B ingress
  rejects both an extra SNI label (`spike-ingress.json` probe `7_tls_handshake_mailhub`,
  TLS handshake fails before any HTTP layer) and an overridden `Host` header on the valid
  ingress hostname (`services-websites.json` `addressing_evidence.caddy_passthrough_probe`,
  `400 Invalid host`). `WEBSITE_HOST_SUFFIX=127.0.0.1.nip.io:8090` via the host proxy (binding
  127.0.0.1:80 was denied on the build host) plus a native-root guest-side proxy on :80/:8090.
  Port 8080 remains exclusively assigned to VLC.

### Local patches to upstream execution (disclosed; applied by runner/setup.sh, proven by --verify)

These change agent-action execution relative to the pinned upstream code and are therefore
recorded as deviations from the reference runtime, not as fidelity fixes. Each is one anchored
string replacement in `runner/setup.sh` (`assert count == 1` against the pin, idempotence
guard); `runner/setup.sh --restore` removes them and `runner/setup.sh --verify` proves the
exact patched state before every run.

| Patch | Upstream behaviour | Local behaviour | Evidence |
|---|---|---|---|
| (e) `mm_agents/m3/parser.py` key table | `super` → `command`; PyAutoGUI's X11 backend has no such key and silently drops the press | `super` → `win` (the X11 Super key) | `docs/sample-run-results.md` task 103 (confirmed misexecuted action) |
| (f) `mm_agents/m3/parser.py` terminal marker | `[INFEASIBLE]` anywhere in the response — reasoning included — returns `FAIL` and ends the rollout | the marker counts only outside `<mm:think>…</mm:think>`; a marker in reasoning no longer overrides a tool call in the same response | `docs/sample-run-results.md` task 067 (marker in reasoning overrode an actual Ctrl+C tool call) |
| (g) `desktop_env/controllers/python.py` action deadline | the client gives up at 90 s and returns `None` while the guest keeps executing to its own 120 s deadline, so typing continues under the next action | the client waits up to 130 s, so the guest's verdict for its 120 s kill arrives before the client gives up | `docs/sample-run-results.md` tasks 093, 059, 079, 082; the 2026-09-14 isolated typing control above (`None` at 90.12 s, completion at 102.61 s) |
| (h) `desktop_env/controllers/python.py` guest-timeout retry | a non-200 is retried up to `retry_times`, so the guest's timeout 500 would replay a partially applied action | a 500 whose body carries subprocess's `timed out after` text breaks out of the retry loop and falls through to upstream's own `return None` — the same value upstream returns on a client-side `ReadTimeout`, with no replay | same tasks as (g); `tests/test_checkout_verification.py` pins the patched text and the anchor's uniqueness in the pin |
| (i) `mm_agents/m3/agent.py` inference errors | exhausted errors return an empty action list, which becomes `ASK_USER` | save the API failure log and propagate the exception so the task receipt fails | `tests/test_checkout_verification.py` |
| (j) `desktop_env/controllers/website.py` URL scheme | a failed HTTPS probe permanently caches HTTP | honor `OSWORLD_WEBSITE_SCHEME=https` for the TLS-only campaign fleet | `tests/test_checkout_verification.py` |
| (k) `mm_agents/m3/parser.py` typing | each character incurs PyAutoGUI's 0.1-second pause, exceeding the 120-second guest deadline for long inputs | one ordered `pyautogui.write(text, interval=0.001)` call, with one final pause | `tests/test_checkout_verification.py` |
| (l) `mm_agents/m3/agent.py` empty actions | empty or unparseable model output becomes `ASK_USER`, even when no question was intended | preserve the API log and fail the task; only an explicit parsed `CALL_USER` becomes a simulator question | `tests/test_checkout_verification.py`; task 019 in the Bedrock sample |
| (m) `mm_agents/m3/agent.py` adaptive thinking | a fixed thinking budget is included with adaptive mode, which Bedrock Sonnet 5 rejects | omit `budget_tokens` for adaptive mode; preserve it for enabled mode | `tests/test_checkout_verification.py`; live Bedrock request validation |

(h) deliberately keeps `None` rather than inventing a success-shaped result: upstream evaluator
getters read `env.controller.execute_python_command(...)["output"]`, so a 200 carrying empty
output would convert an infrastructure failure into a silent zero score. No evaluator code path
changes.

The M3 prompt still forbids asking for clarification and omits the parser-supported
`call_user` action (task 095). `MAX_STEPS`, every agent and evaluator prompt, and every
scoring function remain upstream's. Patch (k) changes typing speed; other PyAutoGUI calls
retain the guest's 0.1-second pause.

With (f), an `[INFEASIBLE]` emitted only inside the reasoning block does not terminate
the rollout. If no executable action accompanies it, (l) retries the response within
the configured limit and then fails the task. The worst-case span of a non-timeout retry inside
`execute_python_command` grew with the longer per-request deadline (three attempts at up to
130 s each instead of 90 s), because (h) short-circuits only the guest's own timeout 500.

An isolated long-typing control exercised (g) and (h) together against one live guest
build — one 3,000-keypress action, the returned value and its wall time, two screenshots
15 s apart, and the guest's own `POST /execute` count. It was run once, on
`osworld-v2-gnome:00124a57-267c-45e7-93d1-ca0ff196e4b8`:
[result](out/osworld-v2-evidence/controls/typing-control-00124a57-267c-45e7-93d1-ca0ff196e4b8.json).
The hand-run script that produced it was a one-off and is not kept in the repository; the
result file records its inputs and acceptance criteria.
The 66,016-character action returned `None` after **120.39 s**, the two screenshots taken 5 s
and 20 s after that return are byte-identical (both in the text-area crop and in the full
frame), and exactly **one** `POST /execute` reached the guest. All three acceptance criteria
hold: the guest killed the action at its own 120 s deadline, block (h) did not replay the
timeout `500`, and typing had stopped by the time the client moved on. Compare the unpatched
control above, which returned `None` at 90.12 s while typing continued to 102.61 s. This is
one control on one build, not a claim about every action a campaign issues.

Patch (k) also has a successful live typing control: one 6,090-character command
completed in 10.47 seconds and produced a 5,700-character file whose SHA-256 exactly
matched the expected text, including quotes, backslashes and newlines. See the
[control receipt](out/osworld-v2-evidence/pr5-bedrock-sample/controls.json).

Upstream issue drafts for (e), (f), and the 90 s-client/120 s-guest deadline mismatch are
prepared under `out/osworld-v2-evidence/upstream-issues/`; they have not been filed.

## Bedrock sample on the current build

Tasks 008, 019, 026, 041, 092 and 095 ran on
`osworld-v2-gnome:00124a57-267c-45e7-93d1-ca0ff196e4b8` with Bedrock Sonnet 5,
the M3 agent, adaptive thinking, 4,096 output tokens, six concurrent workers and
a 100-action limit. All six completed setup, inference and evaluation without
a task-level error: [sample receipt](out/osworld-v2-evidence/pr5-bedrock-sample/sample.json).
Task 092 scored 0.1; the other five scored 0. Task 095 declared failure after
23 actions; the other tasks exhausted the limit. No task retry was used.

This verifies bounded execution, not benchmark performance or scoring parity.
Sonnet repeatedly supplied out-of-bounds pointer positions in four tasks (008, 019, 041
and 092; none in 026 or 095) despite the M3 prompt's normalized-coordinate rule; the
per-action pointer diagnostics stay in the gitignored raw run directory.
This agent/model pairing is not validated for a larger performance run.
The sample did not reach an LLM judge or user-simulator call; their live coverage
comes from the explicit task-loop controls described above.

The [inference accounting](out/osworld-v2-evidence/pr5-bedrock-sample/inference-spend.json)
covers all diagnostic attempts, controls and sample requests: $55.25 estimated
measured usage, plus $40.37 retained reservations for responses without usage,
or $95.62 accounted against the shared $160 cutoff and requested $200 limit.
The estimate uses published prices with a 10% allowance, not an AWS invoice.
Both service fleets and all campaign guests were stopped after verification.
Earlier Haiku attempts, including intentional interruptions and malformed-action
failures, are counted in that accounting; their per-attempt notes stay in the
gitignored raw run directory.

## Not-certified / excluded

- **VNC: absent entirely.** Headless, same as the V1 conversion; the provider's IP/port
  tuple reports `0` for the VNC slot. Never built, never probed. Live view and human
  takeover are scoped as a separate PR in
  [issue #2](https://github.com/mattteufel-e2b/osworld-v2-e2b/issues/2).
- **Screen recording: opt-in, off by default as upstream; verified.** `ENABLE_RECORDING=1`
  passes upstream's `--enable_recording` through; the September 11 task 093 rollout produced
  the guest's `recording.mp4` alongside its trajectory (see that section).
- **Judge and simulator wiring is verified; scoring parity and multiphase coverage remain incomplete.**
  The [Bedrock controls](out/osworld-v2-evidence/pr5-bedrock-sample/controls.json)
  exercise task 035's real spreadsheet evaluator and task 095's real user simulator
  through the upstream task loop. Each makes one successful, measured Haiku 4.5 call.
  They are scripted controls, not agent-performance results. The earlier task 069
  run stopped in phase one, so later-phase execution remains unverified. A matched
  reference run is still required before claiming scoring parity.
- **Pause/resume: not used.** Deliberate no-pause-by-default stance (median task length
  ~1.6 h, tail to ~3 h — a guest clock jump under pause risks breaking scheduled dynamic
  events and drops CDP WebSockets). The validation campaigns did not invoke pause;
  the feature itself remains untested here.
- **Proxy-required tasks: not applicable to this release.** Unlike V1 (which excluded
  `proxy=true` tasks), recon found zero references to a proxy-requirement field anywhere in
  the pinned V2 task set — there is nothing to exclude on this axis.

## Parity promotion-gate stanza

The promotion gate: a same-model, same-config comparison against a published OSWorld 2.0
result, priced before authorization. **This gate is defined and costed here; it is not
executed by this conversion.**

| Field | Contents |
| --- | --- |
| Reference model / agent | Claude Opus 4.8, max-thinking configuration — published 20.6% binary task-completion on OSWorld 2.0's 108-task release. Secondary published reference: GPT-5.5, ~13–14%, same release. |
| Published band | 20.6% (Opus 4.8, primary); ~13–14% (GPT-5.5, secondary) — point estimates as published; no confidence interval is publicly stated for either, unlike the Terminal-Bench 2.0 comparison this repository already has (16.0–21.4%). Treat point-estimate comparison as weaker evidence until an interval is available. |
| Valid-trial count | Not fewer than 1 trial/task (108 rollouts) to reproduce a single comparable point; **recommended minimum for a defensible comparison is 3 trials/task (324 rollouts)** given no published interval to anchor against. Operator sets the final count before authorizing spend. |
| Release pins | `osworld-v2-2026.08.08`; OSWorld-V2 commit `d578d2d4e0dc82b43e270fdaa7fa89d9708cd154`; e2b SDK `2.34.0`; aiohttp `3.14.1`. Select and validate an immutable guest build before the comparison; keep all pins fixed across every trial. |
| Cost estimate | See below. |

### Cost estimate (derived from this conversion's own measured numbers)

This conversion's real-agent rung ran gpt-4o at a **75-step** budget: 10 rollouts, wall
times 704.9–1064.2 s (mean 856.4 s ≈ 14.3 min/rollout,
`out/osworld-v2-evidence/agent-run-summary.json` `tasks[].wall_clock_s`). A genuine parity
attempt needs a far larger step budget — upstream's published rollouts average closer to
**~318 tool calls**, consistent with the reported **median ~1.6 h / tail ~3 h** per-task
duration. Linearly scaling this conversion's 75-step numbers by 318/75 ≈ 4.24× gives
**~50–75 min/rollout (mean ≈ 60 min)** — the same order of magnitude as the median figure
above, but almost certainly an *underestimate*: our scaling is linear in step count and
does not capture per-step context growth, retries, or harder-task step overruns that a
real long-horizon rollout accumulates. Treat the published median 1.6 h / tail 3 h as the
operative planning number, with this conversion's scaled ~1 h figure only as a
sanity-check floor.

- **1 trial/task, 108 tasks, at the median 1.6 h/rollout**: ≈172.8 sandbox-hours.
  The coordinator admits at most 80 workers, so budget two waves, approximately 3.2 h at
  that representative duration, plus startup and the long tail. The successful 201-sandbox
  admission probe does not increase this worker cap; strict resets also consume extra slots.
- **3 trials/task, 324 rollouts**: ≈518.4 sandbox-hours at the median. At 80 workers,
  budget five waves, approximately 8 h at that representative duration, plus startup and
  the long tail. These are planning estimates, not measured Opus 5 completion times.
- **Sandbox compute cost and LLM API cost in dollars are deliberately not estimated here** —
  current E2B sandbox pricing and the chosen model's per-token pricing (Opus 5 with
  max effort is materially more expensive per rollout than the gpt-4o baseline
  this conversion used) were out of scope for this task and must be priced against
  current rate cards before authorization, not assumed from this conversion's numbers.
- This estimate excludes: fleet warm-up (websites fleet first-boot compose build measured
  524 s, `out/osworld-v2-evidence/services-websites.json`), any repair/retry overhead, and
  judge-model calls (their cost contribution is not measured here).

**NOT AUTHORIZED — separate decision.** This stanza defines and prices the gate; it does
not request or imply authorization to run it. A parity campaign requires an explicit
operator decision to spend against the estimate above — reserved for major
upstream revisions or explicit demand, never routine adoptions.
