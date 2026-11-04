# Fast workflows (0.5.0)

The server executes diagnosed rendering without an LLM call per component or
preparation stage. All native work stays sequential on the COM STA under one
CAD lease. Validation, readbacks and image verification remain enabled. Faster
orchestration does not imply a faster GPU at identical rendering settings.

## Discovery and results

`--tool-profile full` is the server default and retains all native and PRO tools.
`--tool-profile compact` exposes ten workflow tools: capabilities, diagnosis,
repair, batch RGB, render jobs, operation/list, result retrieval, tool help and
native plans. Newly generated client snippets select compact; existing client
settings are not edited. `scripts/configure_clients.py --tool-profile full`
generates full-profile snippets.

`pro_tool_help(tool)` returns an individual full contract without CAD calls.
Native contracts remain usable in `pro_run_plan` even in compact mode. PRO
tools remain excluded from native plans; workflow stages use an internal
allowlist rather than recursive tool dispatch.

New workflows default to summaries in either profile; compact also summarizes
native-plan results. Summaries retain status, counts, actionable errors,
warnings, cleanup outcomes, timings and artifact links. Complete evidence is
available through `details_uri`, its local JSON file, or
`pro_result(operation_id, kind="details")`. `result_view="full"` changes
presentation without changing execution identity. Legacy full-profile tools
retain their previous default results.

Poll `pro_operation(operation_id, wait_seconds=20)`. Compact and new-workflow
polls contain no images by default. Fetch the final image once through
`pro_result(operation_id, kind="image")`; select a registered preview with
`artifact_id`. An artifact must belong to that operation. Retrieval never
repeats CAD. Large images retain local paths when the inline budget is exceeded.
Legacy full-profile polls can still attach images for compatibility;
`include_images=false` disables them.

## Diagnosis and explicitly applied repair

`pro_diagnose(document_path, deep=false)` reads the exact active saved part or
assembly. It never switches documents, closes sketches, rebuilds, resolves
components or saves. An unattached server reports its connection state instead
of automatically launching SOLIDWORKS.

Basic checks cover feature errors, sketch status, equation references, assembly
mates, referenced files, component loaded state and active edit state. Warnings
alone do not fail health. `ok=true, healthy=false` is a completed scan that found
problems or incomplete checks. Basic checks cover the active model rather than
every nested component feature tree. A healthy scan does not establish design
intent or manufacturing correctness.

`deep=true` adds native body-fault and assembly-interference checks. Changed
interference-manager options are restored and verified. Native `Done()` can
mark an otherwise unchanged assembly dirty; the strictly verified case is
reported as `DEEP_CHECK_MARKED_DIRTY`. The dirty flag is never cleared or saved.
Other document changes and unavailable checks remain blocking unknown results.

First call `pro_repair(action="plan", document_path=...)`. Optional actions are
`reconnect`, `resolve`, `rebuild`; resolving additionally requires exact
lightweight instance names. The response includes `plan_id`, preconditions,
steps and expected outcomes. Planning never applies a repair.

Only after a separate user instruction call
`pro_repair(action="apply", plan_id=...)`. Native state is rechecked; stale plans
are rejected. Results are retained after every attempt, including timeouts,
preventing silent repeated execution. Reconnect attaches to the running
installation. Resolving affects only explicitly named instances. Rebuild runs
once, followed by diagnosis; remaining errors are reported as unresolved.
Dimensions, equations, mates and replacement reference paths are not guessed.

Plans and request replay protection are process-local. After restarting the
server, inspect native state and previous outputs before submitting a new
mutation. An MCP timeout does not imply that native work stopped.

## RGB and static render jobs

`pro_colorize` takes `document_path`, `request_id` and 1–128 assignments with
`color_rgb: [r,g,b]`, integer channels 0–255. Omit `component` for a whole part;
assemblies require exact top-level instance names. It validates every target
before writing, skips verified matches, reads back each change and redraws once.
On failure it restores prior arrays or removes previously absent overrides,
then verifies restoration. Failed restoration is explicitly uncertain.

Colors remain unsaved in the current configuration. Assembly overrides do not
edit referenced part files. Existing `.p2m` tools remain available; render
appearances can override legacy RGB in Visualize, so RGB readback alone does
not prove the rendered color is correct.

Send this example to `pro_render_job`, replacing the exact active saved path:

```json
{"document_path":"C:\\Models\\Plate.SLDPRT","request_id":"plate-01","assignments":[{"color_rgb":[32,100,210]}]}
```

The job diagnoses, preflights renderer/targets, captures camera, applies colors,
frames, renders/validates a preview, then renders the final. It never repairs
automatically. Blocking diagnosis returns its evidence and, when possible, an
unapplied repair plan. Image validation checks format, dimensions and hashes;
`visual_review_required=true` explicitly retains composition/quality review.

Defaults: preview 960×540/50 passes; final 1920×1080/200 passes; denoiser enabled.
`quality="4k"` chooses 3840×2160 final. `width`, `height`, `passes` override final
settings only; `denoiser` applies to both. Current scene/materials are used. An
active named camera is refused before mutation; deactivate it explicitly first.

`mode="exploded"` requires 1–128 explicit transforms: exact `component`, absolute
`position_mm: [x,y,z]` and `rotation_deg: [rx,ry,rz]`. Normal mode refuses
transforms. Only top-level rigid loaded component poses are supported. No
directions, distances, nested movement or physical disassembly are inferred.
The output is a static illustration, not an animation or motion simulation.

Temporary poses are restored using exact native matrices, followed by the
original model-view camera. Cleanup runs after success, failure or cancellation
before the lease is released. No rebuild occurs after posing; later mate solving
can override poses. Requested colors intentionally remain unsaved. Unverified
pose, camera or renderer-option restoration marks the outcome indeterminate.

## Verification

Run `python -m pytest -q` and `python -m ruff check src tests scripts benchmarks`.
`python -m benchmarks.run_fast_workflows --output artifacts/benchmarks/fast.json`
measures real discovery and separately labelled synthetic envelopes.
[Benchmark evidence](fast-workflow-benchmark.md) distinguishes bytes, calls and
actual model tokens.

`python scripts/validate_fast_live.py --live --render --repeats 3` creates scratch
fixtures in a new directory, tests real diagnosis/repair, compares per-component
and batch RGB, runs real normal/exploded preview/final jobs, and checks cleanup.
It requires an initially empty SOLIDWORKS session and closes only its own files.
