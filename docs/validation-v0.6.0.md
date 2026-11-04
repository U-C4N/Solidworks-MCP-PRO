# v0.6.0 verification — 4 November 2026

This release adds design requirements, isolated parameter studies, product shot
packs and a read-only connection doctor. Automated tests, protocol checks and
native acceptance are separate evidence. Nothing here establishes general
manufacturability, mechanical strength or universal render performance.

## Automated and protocol checks

The final suite contains **1,326 passing tests**. Ruff is clean. The stdio smoke
test initialized `solidworks-mcp-pro` 0.6.0, discovered **123 full-profile tools**,
read the guides/prompts, and correctly rejected an invalid plan. Compact has
**14 workflows**; native tools remain available through validated native plans.

The count above is the complete local Windows run. GitHub's matrix also runs
Python 3.11 and 3.12 on Windows and Linux. Windows-only COM marshalling contracts
use platform-aware skips on Linux; those skips do not remove them from Windows
coverage. An initial Linux run exposed three older unguarded `pythoncom` imports;
they were corrected, and matrix fail-fast was disabled so every environment can
finish and report its own result.

Coverage includes explicit native units, pass/fail/unknown requirements,
read-only metadata checks, exact-part copy isolation, signed dimension readback,
dependent-dimension changes, rejected rebuilds, source hashes/open state,
copy cleanup, request replay/conflicts, report/image ownership, exact-ratio
previews, partial output, cancellation and camera restoration failures.

These tests use synthetic COM objects or deterministic backends. They do not
count as 1,326 native CAD tests. The previous baseline contained 1,074 tests.
One initial suite run found the historical benchmark catalogue expectation still
set to 31 PRO tools; it was updated to the release's 35 and the suite rerun.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts benchmarks
.\.venv\Scripts\python.exe scripts\smoke_stdio.py
```

## Connection doctor found a real mismatch

The local source initially reported 0.5.0 while the active virtual environment's
distribution metadata reported 0.4.0. The doctor returned
`INSTALLED_VERSION_MISMATCH`; it did not silently call that installation ready.
Reinstalling the editable package aligned the environment. After the release
version change, it was installed as 0.6.0 and the running PRO catalogue reported
`status:"ready"`, `healthy:true`, with no identity/profile issues.

The chat's pre-existing `solidworks` plugin exposed a different catalogue with
no `pro_*` tools. That plugin is not evidence of this project's runtime version.
The new tool's live identity claim is scoped to the PRO process serving its
request. Offline client-file inspection cannot independently prove another
chat's active connection.

## Native environment

- Windows, 64-bit Python 3.12.16.
- SOLIDWORKS **2026 SP4.1**, revision **34.4.1**.
- Pinned `solidworks-mcp==0.19.0` CAD backend and installed/licensed Visualize.
- A new SOLIDWORKS session; all probes required an initially empty document list.
- Only newly generated scratch parts/assemblies were edited and closed.

No GPU attribution, equal-quality timing comparison or rendered RGB fidelity
claim is made. Current native scene/materials were used.

## Render prerequisites

The existing native fast-workflow acceptance ran with ordinary `--live --render`
and **without** `--allow-render-unavailable`. Both normal and explicit exploded
jobs produced real 960×540 previews and 1920×1080 final PNGs. Decoded format,
dimensions and hashes were checked. Original camera and exact component poses
were restored and verified; no final followed an invalid preview.

The new standalone probe also produced a real **640×360 PNG at 16 passes** via
`solidworks_in_process_macro`, with `addin_render_verified:true`,
`macro_render_verified:true` and verified fixture cleanup. Its project-renderer
route was not used as a fallback and is not claimed by that result.

The earlier v0.5.0 session's macro failure did not recur in the new session.
This is successful current-session acceptance, **not** a demonstrated root cause
or source-code repair of that historical session failure.

```powershell
.\.venv\Scripts\python.exe scripts\validate_fast_live.py --live --render --repeats 1 --output artifacts\v060-prerequisites\fast-render-20261104
.\.venv\Scripts\python.exe scripts\validate_render_live.py --live --load-addin --output artifacts\v060-prerequisites\render-probe-20261104
```

## Native engineering acceptance

The new script builds an independent **100×70×10 mm** plate, four through holes
of Ø8 mm, and one through hole of Ø24 mm. Its expected volume comes from the
analytic formula, not the measurement under test:

`(100 × 70 − 4 × π × 4² − π × 12²) × 10 = 63,465.48728053323 mm³`.

Verified outcomes:

| Check | Actual result |
|---|---|
| Exact depth dimension | 10 mm; requirement passed |
| Analytic solid volume | 63,465.48728053323 mm³ within 0.001 mm³ |
| Tight Z extent | 10 mm |
| Native mass | 0.06346548728053324 kg at this fixture's effective native density |
| Intentionally incorrect depth requirement | 999 mm expected; completed scan correctly returned `passed:false` |
| Read-only state | Top model metadata/file stat unchanged |

Three independent copies used 6, 8 and 10 mm thickness. All passed rebuilds,
dimension readbacks and volume-range requirements; valid ranking was:

| Thickness | Native mass, kg |
|---:|---:|
| 6 mm | 0.038079292368319945 |
| 8 mm | 0.050772389824426584 |
| 10 mm | 0.06346548728053324 |

The source SHA remained unchanged; its open-document metadata also remained
unchanged. Owned copies closed and the original active document was restored.
Repeating the execution request reused the original operation rather than
creating more copies. CSV/JSON reports were produced. Native mass here depends
on this fixture's effective density and is not a material specification.

A two-component scratch assembly placed the copies 140 mm apart along X.
Independent requirements passed for **240 mm** overall width, **40 mm** native
minimum clearance, and **0.12693097456106647 kg** total native mass. The explicit
component-entity measurement route worked without a selection fallback.

## Native shot pack

Both native routes produced three aspect-matched previews and three real final PNGs,
a labelled contact sheet and a JSON manifest. Accepted final sizes were
**512×512**, **640×360**, and **360×640**. The release's final sample uses an
isometric hero, a front view and a custom portrait direction through the explicit
`project` route.

Visual inspection found that the add-in's native model-view transfer cropped
the wide plate in portrait output; changing model-view zoom did not establish
reliable rendered fit. The release therefore adds opt-in project framing. It
exports once, computes an explicit camera from verified native solid bounds,
and renders owned SVPJ copies with horizontal/vertical FOV fit. All eight bbox
corners are covered by geometric unit tests, and the final three-shot native
package was visually reviewed with the complete plate inside every frame.
This route supports documented world-Y-up views and refuses unsupported views
before CAD calls. It never silently replaces add-in render acceptance.

Every PNG was fully decoded and hashed. Original camera restoration passed.
Only the script's own `EngineeringPlate` and `EngineeringAssembly` were closed;
final cleanup was verified. Contact-sheet composition was visually reviewed.
These are intentionally small acceptance renders, not a benchmark or a promise
that arbitrary automatic views will make attractive product imagery.

```powershell
.\.venv\Scripts\python.exe scripts\validate_engineering_live.py --live --render --render-route project --output artifacts\v060-engineering\acceptance-project-final
```

## Evidence and limits

[Portable native summary](evidence/v0.6.0-native.json) retains numeric results,
hashes, route distinctions and cleanup outcomes without workstation-specific
absolute paths. Full local evidence is under the command output directories;
`artifacts/` is intentionally excluded from Git and the source distribution.
Generated user CAD, local task scripts and private configuration are not shipped.

Native acceptance covers this scratch part, a two-instance assembly, three
linear-dimension candidates and three shots. General angular studies, arbitrary
nested assemblies, all material/scene combinations and every installed version
remain outside that sample. Unit tests cover additional boundaries, not native
compatibility across those untested environments. Replay is still process-local;
plans remain sequential/non-atomic and unrelated CAD clients do not share the
PRO lease.
