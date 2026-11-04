# Engineering and product-shot workflows — v0.6.0

These tools are published in both `full` and `compact`. The current catalogues
contain 123 tools (88 inherited CAD + 35 PRO) and 14 workflows respectively.
Use `pro_tool_help` for the exact machine-readable schema.

Call the native `get_status` first to attach to an already running SOLIDWORKS
session. The connection doctor, design checks and variant bridge do not launch
or implicitly attach to CAD. Renderer loading is explicit through
`pro_visualize_status(load_if_missing=true)`.

## Connection identity

`pro_connection_doctor(include_client_configs=false)` reports the identity of
the Python process serving that request and compares its actual published tools
with its full/compact catalogue. It does not attach to CAD. Optional conventional
client-file inspection reads allowlisted command identity/profile fields, never
executes those commands, and omits raw args, environment values and parser errors.

The standalone module is useful before connecting:

```powershell
.\.venv\Scripts\python.exe -m solidworks_mcp_pro.connection_doctor --source-root . --include-default-clients --tool-profile compact
```

Without a supplied catalogue observation it returns `incomplete`, not evidence
that a different chat is connected. Exit codes are 0 ready, 1 mismatch, 2
incomplete/invalid input. The legacy `--doctor` dependency report is unchanged.
Reinstall the actual environment when distribution metadata differs from source;
do not hide the mismatch by changing the reported version.

## Explicit design requirements

`pro_validate_design(document_path, assertions, result_view="summary")` reads
the exact active saved part or assembly without activation, rebuild, save or
selection changes. Finish open sketches/in-context editing first.

Every assertion requires unique `id`, `kind`, `unit`, `abs_tolerance` and either
`expected` or one/both `min` and `max`. `expected` cannot be mixed with limits.
There may be 1–128 assertions. All numbers must be finite; booleans are not
numbers. `rel_tolerance` defaults to zero and is restricted to 0–1.

| Kind | Additional fields | Unit / meaning |
|---|---|---|
| `dimension` | `name`: exact native dimension name | `mm` or `deg`, verified against native type |
| `mass` | None | `kg`, native effective density/overrides |
| `volume` | None | `mm3`, positive native solid volume |
| `bbox_size` | `axis`: `x`, `y` or `z` | `mm`, tight solid extrema in active-model axes |
| `distance` | `component_a`, `component_b` | `mm`, unsigned distance between exact loaded top-level part instances |

Effective tolerance is the larger of absolute tolerance and relative tolerance
times the largest absolute supplied expected/limit value. Native SI values are
converted explicitly. An angular dimension cannot silently be treated as mm.

`ok:true` means every requested check completed; `passed:true` additionally means
all requirements held. A completed nonconforming check has `ok:true`,
`passed:false`, `status:"fail"`. Unsupported, incomplete or changed-state
measurements have `status:"unknown"`, `ok:false`, and never pass.

Per-assertion results retain actual, requested values, tolerance, status and
measurement source. Top-level counts separate passed/failed/unknown. Before and
after comparison covers the top model's path, configuration, update stamp,
dirty/edit state and saved-file stat; it is not a recursive geometry snapshot.

Assembly mass/bounds require resolved loaded geometry; unsupported/incomplete
components are not silently skipped. Minimum distance does not calculate signed
penetration: zero can mean contact or overlap. This is not interference-free
fit certification, FEA, material certification or a general DFM test.

## Isolated parameter studies

`pro_variant_study(document_path, request_id, parameters, assertions?,
dry_run=true)` supports a saved, nonempty, self-contained local `.sldprt`.
External CAD references are refused. An already open source must be clean;
the tool never saves it to make the precondition pass.

Each of 1–2 parameters supplies an exact `dimension`, explicit `unit` (`mm` or
`deg`) and a finite, unique `values` array. The Cartesian product is at most 16.
Both dimensions must be driving and match their declared native type.

Dry run performs preflight, source/dependency inspection and returns the explicit
combinations. It opens no copy, creates no directory, and changes no CAD state.
For a closed source the dimension check is explicitly deferred until its first
owned copy can be opened. Execute the reviewed definition with `dry_run:false`.

Each candidate starts from a byte-verified independent source copy in a fresh
output directory. Only that copy receives dimension writes, rebuild and save.
All requested dimensions are read back after rebuild and save; silent native
rejection, sign normalization, dependent-dimension changes and feature errors
invalidate the candidate. Native positive mass/volume and solid extrema are
recorded, followed by optional requirements using the same assertion contract.

The study may complete with invalid candidates. Only valid candidates appear
in `ranked_variants`; sorting is ascending mass with a deterministic ID tie-break.
No valid candidate means an empty ranking and `best_variant:null`.

Source SHA-256 is checked before each copy and after the study. If the source was
open, its model metadata is also rechecked. Owned copies are closed; the prior
active document is restored without rebuild. Unverified cleanup/source state is
`indeterminate`. This is limited observed-state protection, not a file-system
transaction or a guarantee against unrelated concurrent automation.

Reports include candidate `.sldprt` paths, `csv_path`/`csv_uri`,
`manifest_path`/`manifest_uri`, counts, ranking and cleanup evidence. CSV uses
UTF-8 with BOM for Excel and is served as text through MCP resources.

## Product shot packs

`pro_shot_pack(document_path, request_id, shots, passes=100, denoiser=true,
render_route="addin")`
supports 1–12 explicit shots of the exact active saved part/assembly. Every
shot has a unique case-insensitive ASCII name (1–40 letters/digits, `_`, `-`),
and a `view`. `iso` aliases `isometric`; standard views and `current` are
supported. `custom` requires `from_direction`, optionally `up_direction`.
These are direction vectors, not camera positions. Optional `zoom_factor` is
0.25–2. The existing checkpoint guard refuses an active named camera.

| Aspect preset | Final size | Usual preview |
|---|---:|---:|
| `square` | 1024×1024 | 640×640 |
| `landscape` (default) | 1920×1080 | 640×360 |
| `portrait` | 1080×1920 | 360×640 |

Supply both `width` and `height` (64–4096) to override a preset. Previews retain
the exact integer-pixel aspect and use at most 25 passes; unusual coprime/narrow
sizes may require a larger preview to preserve the ratio and renderer minimum.
Final passes are 1–5000. No silent crop or stretch changes the composition.

Choose the route explicitly when stable cross-aspect framing matters:

- `addin` (default) transfers the native model-view camera. Actual Visualize
  framing can differ from model-view fit/zoom; portrait output may crop a wide
  model even with a matching preview ratio. Image inspection remains necessary.
- `project` reads verified native solid bounds, exports once with part grouping,
  and renders owned SVPJ copies using explicit perspective cameras. A bounding
  sphere is fitted against the smaller horizontal/vertical half-FOV with 10%
  distance margin. The native camera is independent of model-view zoom/export
  auto-fit. This route requires the compatible installed Offline Renderer and
  2026 grouped export; it never silently falls back to the add-in route.

Project views are `isometric`/`iso`, `front`, `back`, `left`, `right`, or `custom`
with world-Y up (omit `up_direction` or use `[0,1,0]`). The initial project scope
rejects top/bottom/current/trimetric/dimetric and `zoom_factor>1` before any CAD
call. Zoom-out values 0.25–1 move the explicit camera farther away. Default
vertical FOV is 32 degrees. All eight native bounding-box corners must fit the
geometric frustum calculation; this does not certify scene aesthetics or fit
unlisted transparent/light geometry. Project-copy ground shadows are enabled
and reflections disabled through the existing project-renderer defaults.

The workflow validates every shot, diagnoses once, checks renderer availability,
captures one camera checkpoint, then renders each preview and final in sequence.
Only the add-in route changes the model-view framing for each shot; the project
route changes cameras inside the output project copies instead.
Decoded PNG dimensions and SHA-256 open the next stage; a failed preview cannot
start its final. The camera is restored and read back on success, failure and
cancellation before releasing the CAD lease. Later `current` shots reset to the
original checkpoint rather than inheriting the preceding shot's view.

Outputs include per-shot native PNGs, a labelled contact sheet, JSON manifest,
timings and cleanup. Partial verified images remain available after failure and
are labelled partial. The contact sheet is the default image from
`pro_result(kind="image")`; choose a returned `artifact_id` for a particular PNG.
The manifest references the original files and hashes. A contact sheet is
presentation post-processing, not an additional native render.

The initial pack changes no geometry, poses, colors, scene or saved model. It
uses current material/scene state. Explicit exploded rendering remains available
separately through `pro_render_job`; automatic disassembly is not inferred.
Integrity checks do not decide aesthetic quality or prove RGB/P2M fidelity.

## Lifecycle and evidence

Variant execution and shot packs return a running `operation_id`; poll
`pro_operation(wait_seconds=20)`. Fetch images once through `pro_result`.
Presentation-only `result_view` is excluded from operation identity; conflicting
execution arguments with the same request ID are rejected. A variant dry run
does not consume the execution request key. Replay remains process-local.

All native work stays serialized on the existing COM STA under the PRO lease.
Request timeout/cancellation is not evidence that native work stopped; do not
resubmit uncertain CAD work. Summary responses keep counts, failures, cleanup,
reports and a bounded sample; full evidence stays in `details_uri` and
`pro_result(kind="details")`.

## Reproduce native acceptance

```powershell
# Empty running SOLIDWORKS session; generated scratch parts only.
.\.venv\Scripts\python.exe scripts\validate_render_live.py --live --load-addin --output artifacts\render-check-new
.\.venv\Scripts\python.exe scripts\validate_engineering_live.py --live --render --output artifacts\engineering-check-new
```

The add-in probe never silently falls back to the project renderer. A separate
`--route project --project C:\Models\Example.svpj` probe works on a new project
copy without CAD attachment. Its success does not establish add-in/macro health.
Every invocation requires a new output directory; failures retain their evidence.
