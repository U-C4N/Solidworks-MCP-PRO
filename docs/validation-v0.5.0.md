# v0.5.0 validation — 3 November 2026

The new workflows, compact profile, repair planning and RGB batching are
implemented. Automatic tests and native scratch checks are reported separately.
**New native preview/final rendering was not verified in this session:** the
running SOLIDWORKS process refused both the new and existing fixed SWB macro
route. No generated image or render speed is claimed from those failed jobs.

## Automatic and protocol verification

The implementation started with 920 passing tests. The integrated suite passed
**1,074 tests**, with Ruff clean. Tests cover failed rebuild/mate/equation
postconditions, warnings versus unknown checks, read-only diagnosis, stale and
replayed repair plans, complete RGB preflight, absent-override rollback,
readback/cleanup failures, preview validation before final, cancellation and
CAD lease lifetime, exact active-document boundaries, full-profile compatibility,
real stdio compact discovery, image-free polling and on-demand evidence retrieval.

An additional native-discovered camera defect was corrected: native Scale2
changes the model-view pan, so restoration now applies orientation, scale, then
translation. The regression simulates that coupling. Native before/after
readbacks confirmed the corrected camera restoration.

The existing backend target-document guards remain in legacy APIs. Only the
internal job's explicitly marked exact-active calls bypass retargeting; each
native bridge verifies its requested active path. Rendering consumes and checks
an internal document path before invoking the existing renderer.

## Native scratch acceptance

Command:

```powershell
.\.venv\Scripts\python.exe scripts/validate_fast_live.py --live --render --allow-render-unavailable --repeats 3 --output artifacts/fast-live/v050-acceptance-03
```

The explicit `--allow-render-unavailable` option accepts only the recorded
`VISUALIZE_MACRO_RUN_FAILED` failure for cleanup testing. It does not count a
failed render as a render success: evidence has `render_verified:false` and
`render_validation:"native_macro_unavailable_cleanup_verified"`. Without that
option the native validator fails on an unsuccessful render.

Environment: Windows, Python 3.12.16, SOLIDWORKS 2026 SP4.1 (revision 34.4.1),
the pinned 0.19.0 CAD engine, MCP 1.30.0 and Pillow 12.3.0. The test required an
initially empty SOLIDWORKS session. It created its own 40×25×8 mm solid part and
four-component assembly, saved only its fixture files, and closed those scratch
documents. No existing user model was changed.

Verified native cases:

- Part deep diagnosis: healthy, approximately 185 ms in this one observation.
- Explicit repair plan: no native mutation; separate apply: rebuild and fresh
  healthy diagnosis. No automatic apply occurred.
- Part and four-component RGB batches: requested arrays read back correctly.
- Assembly deep diagnosis: four bodies checked, zero interferences, healthy;
  the native cleanup dirty-flag effect was explicitly reported as a warning.
- Normal job failed at the macro dispatch; its original camera was restored and
  verified. Exploded job failed at the same stage; all exact native matrices and
  the original camera were restored and verified. Final pose inventory matched
  the initial matrices within 1e-9.
- No final render followed an invalid/missing preview. No native render was
  described as successful or automatically retried.

A separate native check cleared only the adapter's internal session target
while retaining the active scratch assembly. The new exploded job still reached
the expected macro failure and verified both pose and camera restoration, without
retargeting or repopulating that session target. Evidence is retained under
`artifacts/fast-live/v050-exact-active-target-none/evidence.json`.

## Native RGB comparison

Both routes applied the same four RGB assignments to the same assembly fixture.
Before each route the same batch reset restored the starting colors; reset
time is excluded. Three repetitions alternated legacy-first and batch-first
order. The baseline uses the retained `set_appearance` tool once per component;
the new route uses one `pro_colorize` call. Times include the local application
call, validation, COM and returned evidence; they exclude LLM inference and
MCP stdio transport. They are not a competitor benchmark.

| Route | Samples ms | Median ms |
|---|---|---:|
| Four individual RGB calls | 178.974 / 177.748 / 174.761 | 177.748 |
| One four-target RGB batch | 27.975 / 24.965 / 27.552 | 27.552 |

This small fixture's batch median was approximately **6.45× faster** (84.5%
lower elapsed time). The result does not establish the same gain for arbitrary
models, P2M materials, render time or model tokens. Native RGB verification is
separate from Visualize appearance fidelity.

## Native limits and retained evidence

Deep interference cleanup calls native `Done()`, which can set the document's
dirty flag without changing its update stamp. A targeted probe isolated this
effect. The implementation classifies only that exact verified metadata change
as a warning; changed file/configuration/stamp/edit state remains unknown and
blocking. It preserves the dirty flag and records before/after evidence.

The running application returned `RunMacro2=(False,0)` without a completion
marker for the new job, retained legacy render path and a separate basic
marker-only SWB probe. The macro template matches the previously successful
0.4.0 source. No exact cause or repair is established. No macro security setting
was relaxed and the user's application was not restarted. The current limitation
must be resolved and ordinary `--live --render` rerun before claiming successful
new native preview/final output, rendered RGB fidelity or native render speed.

Raw acceptance file: `artifacts/fast-live/v050-acceptance-03/evidence.json`.
SHA-256: `697a81304f8d16c9cda07790d349518eb8d1157cb9a8feb904e412a7fe4dcbee`.
Earlier failed attempts and diagnostic probes remain under acceptance-01 and
acceptance-02; they were not rewritten. Source packaging excludes `artifacts`.
Retain the files separately or regenerate the native fixture check. See the
[discovery and synthetic envelope report](fast-workflow-benchmark.md) for the
separate catalogue/call/byte measurements; actual LLM tokens were not measured.
