# Visualize 4K observability and test review

Static source review, 2026-11-03. This review does not call SOLIDWORKS, Visualize,
COM or a GPU probe. It describes the current implementation and proposed work;
it is not evidence of a completed render or a performance improvement.

## Current 4K behavior

- `pro_visualize_render` accepts width/height 64–4096 and passes 1–5000. The
  internal bridge permits width/height up to 8192 and passes up to 10000. Public
  3840×2160 output is therefore supported by both validation layers. The
  internal limit does not make 8K available through the public tool.
- The bridge verifies the decoded image format and exact requested dimensions,
  then records file bytes and SHA-256. Successful `Render()` alone is insufficient.
  This checks output integrity, not composition, materials or visual quality.
- Inline MCP attachments have a combined **10 MiB of original image bytes**
  budget per response. A larger image retains its successful operation result
  and local path with an attachment warning. Base64 increases wire size by
  roughly one third; the budget is not a 10 MiB transport-message cap.
- MCP resource reads permit up to 32 MiB. Larger output remains available by
  local path. PNG compression varies with image content; pixel dimensions alone
  cannot predict whether a 4K file fits either limit.
- Completed-operation polling can attach the image again. Repeated polling of
  an already completed operation measures extra file reads, encoding and
  transport, without another render. Stop polling when completion is received.
- Current camera, scene and appearances come from the active model. The bridge
  exposes no material/scene editing or GPU/device selection parameters. This
  is an API surface limitation, not evidence that Visualize itself lacks them.

Relevant implementation: `src/solidworks_mcp_pro/server.py` (`pro_tools`,
`_result`), `visualize_bridge.py` (`render`, `_verify_images`), and `storage.py`
(`ArtifactStore.read`).

## Settings restoration and ownership

The bridge reads all nine previous `RenderOptions` values before its first
write: format, passes, dimensions, name, output folder, denoiser, termination
mode and alpha. It checks each write by reading it back, then restores every
prior value in reverse order whether setup, rendering or verification fails.
Restoration failures produce `RENDER_SETTINGS_RESTORE_FAILED`; they never claim
a successful, fully restored operation. Scene and material properties are not
written by this bridge. Native side effects beyond these properties require
separate acceptance evidence.

The background operation owns the shared CAD lock/lease for the full call and
survives request cancellation. A running render must be polled, not resubmitted.
Replay protection lasts for one server process; process termination does not
prove that native rendering stopped.

Existing tests cover PNG/JPEG validation, wrong dimensions, corrupt/missing
files, render false/exception, restoration after those failures, refusing
existing output, request replay/conflict, and oversized inline attachment
fallback. Important focused additions, using fake objects only:

1. A prior-option getter fails: no option writes and no `Render()` call.
2. A setter or its read-back rejects a mid-list setting: all earlier settings
   restored, later settings not applied, no `Render()` call.
3. A restore setter/read-back fails after valid image output: stable restoration
   error, output directory retained, every remaining restore still attempted.
4. Render and restoration both fail: retain both diagnostic causes without
   reporting success.
5. A small valid PNG plus a separate oversized artifact exercises the combined
   inline budget; the first image and the large image's local path both survive.

## Proposed production timing fields

No production timing code was changed during this review. A minimal subsequent
change can use `time.perf_counter_ns()` in the existing synchronous bridge. It
must preserve the current try/finally restoration and output validation.

On verified success, add:

```json
{
  "timings_ms": {
    "setup": 0.0,
    "native_render": 0.0,
    "verify_output": 0.0,
    "restore_options": 0.0,
    "total": 0.0
  },
  "timing_scope": "Visualize bridge monotonic wall time; Render() includes renderer-managed preparation/output, not isolated GPU execution"
}
```

The numbers above are placeholders illustrating the schema, not measurements.
`setup` starts at bridge entry and ends after option application; `native_render`
times only the blocking `manager.Render()` call; `verify_output` covers image
decode, dimensions and hash; `restore_options` covers restoration; `total` ends
after restoration. Total can exceed the sum of recorded stages because of
ordinary Python work. Missing/unreached stages must remain absent or null, not
fabricated zero-time measurements. Use a fake monotonic clock in tests to
check the exact boundaries without sleeps or machine-speed thresholds.

These timings exclude queue/lease wait, artifact registration, image attachment
encoding and MCP transport. Registry `duration_ms` covers the operation from
scheduling through completion, including queue wait and registration. Client
wall time additionally depends on submission, polling cadence and transport.
Keep all three scopes distinct. The API does not expose import time separately;
do not derive it by subtraction or call it zero.

## Performance evidence and GPU telemetry

Compare repeated renders only after recording the same model revision/hash,
camera/scene/material identity, resolution, passes, denoiser, format, software
revision, hardware and execution mode. Record cold/warm state and any concurrent
work. A change in passes, denoiser or scene changes the work and cannot establish
an equal-quality speedup by timing alone. Report the individual samples and
median; one 4K render establishes completion, not stable throughput.

GPU hardware inventory is not proof that a render used that device. GPU mode,
device, usage, memory and telemetry availability must be independently recorded
if claimed. Useful opt-in telemetry acceptance cases are:

- Device inventory is available but process attribution is unavailable: report
  hardware presence and **GPU execution unverified**.
- Samples identify the rendering process/device and overlap its measured
  interval: retain raw timestamps, process identity, device identity and sample
  intervals; utilization remains sampled evidence, not GPU kernel time.
- Probe unsupported, permission denied, GPU idle, or samples missing: retain
  the diagnostic and use unknown/unverified; never silently infer CPU mode or
  zero GPU cost.
- Multiple GPUs or unrelated GPU workloads: aggregate utilization alone cannot
  attribute the render to a particular device.

The existing verified image is the deliverable. A smaller presentation preview
can be added separately to avoid repeated large attachments, while preserving
the original 4K image and its hash. Any resizing time belongs to presentation
post-processing, not the native render measurement.
