# OSWorld2 on E2B: verification findings and merge recommendation

## Merge recommendation

**This is an incremental, experimental port, not a customer-ready full-suite release.** The PR includes the verified judge fixes, simulator configuration checks, host-dependency checks, and verification evidence. It preserves upstream agent and evaluator implementations. A reproducible process-group cleanup defect and the compatibility issues below remain unresolved.

The live verification reviewed [PR #1](https://github.com/mattteufel-e2b/osworld-v2-e2b/pull/1) at base commit `5d359b19e78b7f46ad3f41218612a68c5e0fbce9` with the judge changes included in this PR. The campaign and 303 passing local tests validate those changes. Passing CI does not cover all failures found by the live verification.

The requested delivery scope includes:

1. The tested judge, simulator-configuration, host-dependency, and documentation fixes.
2. The full-inference receipts, diagnostic controls, and explicit verification limits.
3. Follow-up work for process cleanup, application compatibility, browser capabilities, and deployment URLs.

The maintainer has chosen to defer the remaining fixes until after this incremental PR ships. The review recommendation remains to repair process cleanup before relying on its lifecycle guarantees; deferral accepts a known correctness risk. Application compatibility, required browser APIs, and broken deployment URLs remain release blockers for a customer-ready, faithful full-suite claim. These environment issues predate this PR.

Resource assumptions are excluded from this recommendation and follow-up list at the user's request. No new benchmark, scoring framework, or scheduler is required to address the issues below.

## Full-inference verification

The campaign ran 36 selected tasks from the pinned 108-task release, using upstream M3 with Fireworks `accounts/fireworks/models/minimax-m3`, a 500-turn cap, and an eight-hour task deadline. There were no task retries or prediction resampling; upstream SDK transport retries remained enabled. Claude Haiku 4.5 through AWS Mantle served as the LLM judge and user simulator. This is an experimental judge configuration, not a published benchmark judge-parity claim.

Upstream was pinned to `d578d2d4e0dc82b43e270fdaa7fa89d9708cd154`, release `osworld-v2-2026.08.08`. The guest template was `osworld-v2-gnome:0d796343-1a70-4bd3-990e-8bb469b3dd20`. All 108 task hashes were verified. Task goals, scoring rubrics, and agent implementations were preserved.

- 36 original attempts: **34 native evaluations and two unscored failures**.
- **No full-score successes**, nine positive partial scores, and 25 zero scores.
- Thirty tasks exhausted 500 turns; four ended through native agent termination. No task reached its eight-hour deadline.
- All 34 recorded scores exactly matched upstream `result.txt`.
- The original campaign verification gate correctly failed because tasks 003 and 048 were unscored.
- The mechanical mean over the 34 scores was 0.074004. Environment defects prevent treating it as a clean model-performance estimate.

### Task results

Scores are rounded for display. A task without a listed caveat is not certified equivalent to the reference runtime.

| Task | Turns | Native score | Observed caveat |
| --- | ---: | ---: | --- |
| 003 | 357 | Unscored | Agent killed desktop control server |
| 007 | 500 | 0 | |
| 008 | 500 | 0 | |
| 011 | 500 | 0 | |
| 015 | 500 | 0.1449 | |
| 019 | 500 | 0 | |
| 023 | 500 | 0 | |
| 024 | 500 | 0 | |
| 025 | 500 | 0 | |
| 026 | 500 | 0 | Active task state contains inaccessible asset URL |
| 031 | 470 | 0 | Upstream parser treats an infeasibility marker in thinking as FAIL |
| 034 | 500 | 0 | |
| 035 | 500 | 0 | Notification permission denied on HTTP origin |
| 038 | 500 | 0 | Required browser clipboard API unavailable |
| 041 | 500 | 0 | Hardcoded external GitLab URL |
| 046 | 500 | 0 | |
| 048 | 0 | Unscored | Setup archive upload timed out; separate Linux rerun scored zero |
| 050 | 500 | 0 | |
| 053 | 500 | 0.2013 | |
| 057 | 500 | 0 | |
| 059 | 500 | 0 | |
| 067 | 500 | 0 | Missing launcher and incompatible MuseScore version |
| 069 | 500 | 0 | Native score gate stopped at phase 1 |
| 074 | 500 | 0.10 | Many long typing actions and command failures |
| 078 | 500 | 0.35 | |
| 079 | 500 | 0 | WPS Presentation missing at setup |
| 082 | 500 | 0 | |
| 083 | 500 | 0.48 | |
| 087 | 500 | 0.01 | WPS Presentation missing at setup |
| 092 | 500 | 0.25 | Blender missing at setup |
| 093 | 500 | 0.63 | |
| 095 | 65 | 0 | Native FAIL marker; agent never asked simulator for withheld files |
| 098 | 500 | 0 | |
| 103 | 500 | 0 | |
| 105 | 413 | 0.35 | Native DONE |
| 107 | 283 | 0 | KiCad installer failure and native FAIL marker |

Task 003 executed `pkill -9 -f python3`, terminating the user-owned desktop control server. Command and screenshot requests then returned 502; upstream raised a TypeError while writing a missing screenshot. A fresh same-template guest reproduced screenshot HTTP 200 becoming 502 while E2B envd remained available. This is an agent-triggered control-server failure, not a judge outage. Its behavior on QEMU was not tested.

### Runtime comparison

**No matched QEMU, Docker, or VMware baseline was run.** These results establish neither guest-runtime score parity nor a Firecracker-specific cause for low scores.

A separate task-048 control changed the coordinator from the Mac host to a fresh Debian Linux runner on E2B. Both attempts used an E2B Firecracker desktop guest with the same immutable template, task, assets, agent, judge, and turn budget. Concurrency also changed, so this does not isolate host, network, and load effects.

| Task 048 | Mac coordinator, 36-worker campaign | Linux coordinator, isolated rerun |
| --- | --- | --- |
| Setup archive | All five native 600-second attempts failed | All 274,509,060 bytes uploaded and SHA256 verified |
| Agent execution | None | 500 turns, 499 actions |
| Native score | Unscored | 0.0 |
| Receipt gate | Failed | Passed |

The Linux rerun completed in 7,008.8 seconds, approximately 117 minutes. Its 499 screenshots were valid, and all 500 recorded model responses were nonempty. It still logged two desktop-command failures. The rerun demonstrates that the task can complete through this E2B path; it does not erase the original failed attempt or prove general Linux performance superiority.

## Included fix: judge and simulator validation

The fix uses upstream clients and scoring. It rejects empty judge or simulator results before counting success, preventing upstream-swallowed failures from looking like valid evaluations. Startup checks exercise a tiny-budget text answer, an image-reading answer, and the selected tasks' simulator configurations before guest admission. Preflight rejects simulator environment-variable aliases that upstream silently ignores and requires the native evaluator tools `ffprobe` and `identify`.

Relevant files: [model checks](../runner/check_models.py), [model-call tracking](../runner/evaluator_model_calls.py), [preflight](../runner/preflight.py), and [coordinator](../runner/run_agent_parallel.sh).

Validation passed 43 judge controls and six task-specific LLM simulator controls. All nine actual campaign judge calls and all three actual LLM simulator calls succeeded. The prior Fireworks M3 judge configuration produced an empty response at a tiny output budget; a reasoning-disabled M3 configuration also incorrectly awarded full credit to four of five blank-slide negative controls. The documented alternative uses Mantle Haiku, whose tested controls passed. A nonempty response alone is insufficient evidence of judge correctness.

The implementation, regression tests, corrected configuration documentation, and live evidence are included. CI must pass on the resulting PR commit before merge.

## Deferred correctness fix: process-group cleanup

The PR introduces [runner/worker_lib.sh](../runner/worker_lib.sh). Its `terminate_process_group` function checks only the leader PID before signaling and while waiting. Normal-exit and relay shutdown paths also rely on leader liveness.

- If the leader already exited, surviving children can receive no termination signal.
- If the leader exits after TERM while a child ignores TERM, the helper can conclude cleanup succeeded and skip KILL.

Both surviving-child cases were reproduced with the real helper. This is a correctness defect in the PR's lifecycle guarantees, not merely a missing benchmark capability.

Acceptance: check and clean up the process group independently of leader liveness, while preserving handling for the brief interval before the child establishes its session. Add regressions for normal leader exit, timeout, and TERM-resistant descendants. Verify no descendant or occupied worker port remains on those paths. A new lifecycle framework is unnecessary.

The completed verification campaign itself left no matching processes or campaign sandboxes; all 146 checked ports were free. The reproduced defect is an edge case, not evidence that this campaign leaked resources.

## Customer follow-up: application compatibility

Fresh immutable-template inventory and actual task setup logs found the following issues in the [desktop template](../template/template.ts).

| Tasks | Exact incompatibility | Required correction and acceptance |
| --- | --- | --- |
| 067 | Task invokes absent `musescore`. Installed `musescore3` is 3.2.3 and rejects the input created by MuseScore 4.6.5, producing no export. | Install a compatible version and expected launcher. Confirm the pinned input opens and exports through the native task path; an alias alone is insufficient. |
| 079, 087 | Tasks explicitly invoke missing `wpp`, WPS Presentation. | Provide the expected application and verify opening, editing, saving, and native evaluation. Installed LibreOffice is not a validated replacement. |
| 092 | `blender` is absent at setup. | Include a compatible Blender installation and exercise the pinned task's native setup and evaluation. |
| 107 | `kicad` is absent. Native installation returns APT code 100 because held FreeCAD packages prevent compatible dependency resolution. Upstream setup continues despite the failed command. | Resolve compatible KiCad and FreeCAD packaging together; verify both applications and make failed required setup visible. |

Two fresh guests reproduced the KiCad installation failure. An APT dry-run failed with FreeCAD held and succeeded after unholding, but proposed removing FreeCAD and OpenCascade 7.5 components and installing KiCad/OpenCascade 7.6. A full corrected installation was not tested. Simply unholding FreeCAD could break another task dependency.

These are preexisting environment defects. Do not conceal them by excluding tasks or changing applications without validating the original task contract.

## Customer follow-up: browser capabilities

The [fleet routing](../services/hostmap_proxy.py) exposes HTTP origins such as `http://teamchat.127.0.0.1.nip.io:8090`. Actual Chrome probes on TeamChat, CloudCRM, MailHub, and StreamView found `isSecureContext=false` and `navigator.clipboard` undefined. Notification permission was denied, and all ten task-035 attempts to grant notifications failed.

The pinned CloudCRM `Files.jsx` and TeamChat `Message.jsx` copy handlers call `navigator.clipboard.writeText` without a usable fallback. Those actions cannot work as written; task 038 uses those applications. `crypto.randomUUID` was also unavailable, but no affected task behavior was established from that observation.

Acceptance: configure trusted/secure application origins and browser permissions so the required APIs work through the existing routing. Test the actual copy handlers and task-035 notification flow. Preserve application behavior and origin isolation. Cookie-isolation checks passed for 22 fresh sessions across 14 tasks, but they do not establish browser-feature equivalence.

## Customer follow-up: deployment-specific task URLs

### Task 026: inaccessible active download

The active `dynamic_state_026.json` links to:

```text
https://huggingface.co/datasets/xlangai/osworld_v2_file_cache/resolve/main/task_098/AI-Assisted_Healthcare.zip
```

The URL returned HTTP 401 anonymously and 403 with the supplied Hugging Face token. TeamChat's pinned `fileHandler.js` opens absolute HTTP URLs directly; the fleet does not rewrite this link. A separate unused `state.json` contains a current URL returning HTTP 200. The pinned archive is also available in local task assets. The current URL's response content was not hash-verified, so it is not yet a verified drop-in replacement.

Acceptance: apply a narrow deployment/asset compatibility correction to the state actually loaded, verify the downloaded content against the pinned archive, and exercise the in-browser download. Preserve the task's instructions and expected file. Similar legacy URLs in task 050's unused state were not established as an active defect.

### Task 041: external GitLab navigation

The task opens `https://54.174.16.65.sslip.io/`, which times out, while setup and evaluation use the configured E2B GitLab. The instruction does not give the agent the correct deployment URL.

Acceptance: resolve navigation to the same GitLab deployment used by setup and evaluation, then verify the native browser workflow and evaluator. Use a deployment-URL compatibility correction or upstream fix without changing the task goal.

## Additional correctness follow-ups

### Native typing and controller deadlines

The unchanged upstream M3 parser emits one PyAutoGUI keypress per typed character. With the guest's default 0.1-second pause, a 1,000-character control returned no result at the controller's 90.12-second timeout while execution continued; the completion marker appeared by 102.61 seconds. The relevant parser and controller function bodies match pinned upstream.

The campaign logged 350 native command-failure messages across 25 tasks and contained 343 actions with more than 900 keypress calls. The logs do not establish that all failures share this cause, and message counts are not unique failed-action counts. Successful inference and a passing receipt do not prove every issued desktop command completed.

Follow-up: reproduce the same control on the reference runtime and determine an upstream-compatible resolution. Do not silently change typing speed, timeouts, retries, or prompts and then claim equivalent benchmark behavior. None of those were changed for this campaign.

### Native termination, control-server survival, and phase coverage

Tasks 031, 095, and 107 terminated because the upstream M3 parser treats `[INFEASIBLE]` anywhere in a response, including thinking preceding an ordinary proposed action, as FAIL. Task 095 never asked the simulator for the files its task deliberately withholds. These observations explain termination but do not establish an E2B conversion defect.

Task 003's process-kill failure merits a reference-runtime comparison and a review of control-server ownership/lifecycle before selecting a compatibility fix.

Task 069 reached only phase 1; later phases were not covered by its full-inference rollout. The preexisting runner counter deduplicates step numbers across phases and can underreport multiphase work. The independent artifact audit uses phase plus step; this issue did not reduce task 069's reported count because it never advanced. A focused counter correction should preserve upstream scoring.

## Evidence and limits

The artifact audit checked 16,588 trajectory entries, 16,565 action steps, and 23 ASK_USER entries with nonempty replies. It found 16,565 valid 1920×1080 screenshots, no missing referenced screenshots, and one additional empty image from task 003's failed observation. Logs recorded 16,589 nonempty M3 responses and no inference exceptions. These are recorded events, not billed-request or token counts.

All 303 local tests passed, including 19 lifecycle tests. Lint, shell syntax, pinned-source checks, task hashes, and diff whitespace checks passed. Formatting passed for the reviewed scope; a preexisting untracked planning document was the sole full-workspace formatting exception. The existing lifecycle suite passing does not invalidate the separately reproduced cleanup edge cases.

The targeted sample is not a full 108-task agent benchmark or a randomized performance estimate. Audio helper checks do not establish audio-rendering parity, and no matched reference-image application/font/profile comparison or alternative guest-runtime score baseline was completed.

Public evidence:

- [Original campaign receipt](../out/osworld-v2-evidence/sample-36/agent-main36-m3-500-20260914.json)
- [Task manifest](../out/osworld-v2-evidence/sample-36/manifest-36.json) and [run settings](../out/osworld-v2-evidence/sample-36/run-settings-20260914.json)
- [Artifact audit](../out/osworld-v2-evidence/sample-36/audit-main36-20260914.json) and [inference audit](../out/osworld-v2-evidence/sample-36/inference-main36-20260914.json)
- [Judge controls](../out/osworld-v2-evidence/sample-36/judge-controls-20260913.json) and [runtime controls](../out/osworld-v2-evidence/sample-36/runtime-controls-20260914.json)
- [Separate Linux task-048 receipt](../out/osworld-v2-evidence/sample-36/agent-linux-048-m3-500-20260914.json)
- [Local verification](../out/osworld-v2-evidence/sample-36/local-verification-20260914.json) and [cleanup verification](../out/osworld-v2-evidence/sample-36/cleanup-20260914.json)

Raw trajectories, screenshots, gated task contents, and diagnostic artifacts remain in ignored local storage. They should not be included when publishing this report and its sanitized evidence.
