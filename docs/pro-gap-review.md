# PRO gap review and regression evidence — 3 November 2026

This review checked the implemented PRO server against reproducible safety and
verification requirements. It is not a tool-count comparison or a claim that
every competitor was run successfully. All tests described here use synthetic
backends and temporary files; they do not attach to SOLIDWORKS. Live benchmark
results are separate evidence.

## Reproduced before changes

Running the first version of `tests/test_regressions.py` against the original
PRO implementation produced **14 failed tests and 1 passed test**. Several
cases test the same defect with different inputs; they are not 14 distinct
product defects. One failing case specified the new explicit-overwrite API.

| Finding | Reproduction before correction | Implemented correction |
|---|---|---|
| Loose nested geometry inputs | Point arrays containing `"NaN"`, `"Infinity"` or `true` reached the backend because upstream list schemas had untyped elements. | Known point/vector/radius schemas now require numeric elements and documented dimensions. Ordinary text containing `NaN` is still valid. |
| JSON integer exceeds COM numeric range | `10**309` was JSON-serializable and passed a `number` schema, then reached the backend. | Recursive input validation rejects nonfinite values and integers that cannot be represented as finite doubles. |
| State-independent error discovered after a previous mutation | A plan created a new part before discovering that the following box had negative width. | Known size constraints and supported cross-field relationships are checked for every step before execution. |
| Broken geometry did not stop a plan | A synthetic `set_dimension` result with `ok:true` and either `rebuild_ok:false` or a nonempty `broken_features` list was treated as success; the next step ran. | Native results and plan responses expose `CAD_POSTCONDITION_FAILED`, preserve the reported evidence and stop further plan steps. Existing changes are explicitly retained. |
| Existing output was silently replaced | Native save, assembly save, export, screenshot and drawing calls replaced a sentinel file. Save calls with omitted native suffix also replaced their normalized target. | The native adapter checks the actual normalized destination immediately before upstream execution. Existing output is refused unless `overwrite:true` is explicit. The PRO flag is consumed before forwarding. |
| Per-component STL collisions were missed | Checking only the requested assembly filename would overlook other STL filenames selected by SOLIDWORKS. | A default per-component export requires an empty output directory; explicit overwrite permits replacement. |

The compatibility control—negative point coordinates and a signed named
dimension value—passed before and after the changes.

## Evidence behind the added constraints

The implementation is pinned to `solidworks-mcp==0.19.0`. Constraints were
checked against that release's source, rather than inferred from parameter
names alone:

| Contract | Source evidence |
|---|---|
| Box/cylinder sizes are positive | [`session.py`, `add_box` and `add_cylinder`](https://github.com/hjbaard/SolidWorks-MCP/blob/v0.19.0/src/solidworks_mcp/session.py#L870) |
| Cone top diameter may be zero but must be smaller than bottom diameter | [`session.py:1464`](https://github.com/hjbaard/SolidWorks-MCP/blob/v0.19.0/src/solidworks_mcp/session.py#L1464) |
| Rib thickness is positive and its specific `z_mm` offset is nonnegative | [`session.py:2003`](https://github.com/hjbaard/SolidWorks-MCP/blob/v0.19.0/src/solidworks_mcp/session.py#L2003) |
| Loft profile/height counts match; first height is zero; later heights increase | [`session.py:1844`](https://github.com/hjbaard/SolidWorks-MCP/blob/v0.19.0/src/solidworks_mcp/session.py#L1844) |
| Existing-sketch extrusion takes exactly one of depth or `up_to` | [`session.py:4608`](https://github.com/hjbaard/SolidWorks-MCP/blob/v0.19.0/src/solidworks_mcp/session.py#L4608) |
| Optional cut depth may be `null`, meaning through-all | [`server.py`, `cut_profile`](https://github.com/hjbaard/SolidWorks-MCP/blob/v0.19.0/src/solidworks_mcp/server.py#L516) |
| `set_dimension` can report `ok:true` alongside failed rebuild/new broken features | [`session.py:4259`](https://github.com/hjbaard/SolidWorks-MCP/blob/v0.19.0/src/solidworks_mcp/session.py#L4259) |
| Upstream save/export uses silent SaveAs and can replace existing output | [`session.py:366`](https://github.com/hjbaard/SolidWorks-MCP/blob/v0.19.0/src/solidworks_mcp/session.py#L366), [`export`](https://github.com/hjbaard/SolidWorks-MCP/blob/v0.19.0/src/solidworks_mcp/session.py#L5787) |

The source review also removed proposed restrictions that were stronger than
the native contract: empty height lists remain valid for read-only mesh
queries, a negative minimum-edge-length filter is preserved, and a bend
radius that the native straight/smooth-path implementation ignores is not
rejected merely for being negative. Geometry-dependent bend-radius checks
remain upstream's responsibility. Unknown tools retain their supplied schema.

## After-change regression checks

The review suite now includes **42 regression/compatibility cases**, all
passing at handoff. Combined with the 30 workflow tests, **72 tests passed**:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_regressions.py tests/test_workflow.py -q
# 72 passed
```

Additional checks cover recursive numeric validation, nullable cut depth,
negative coordinates/angles, uniform 2D or 3D plane profiles, valid zero cone
tip, ordinary text, unknown schemas, optional empty queries, and unchanged
upstream result evidence. A completed save plan replays its cached result
without rejecting the output it already created. Two steps targeting the
same normalized output without overwrite permission are rejected before the
first write. File checks run again at actual execution under the PRO CAD lease.

## Remaining gaps and claim boundaries

- **Replay protection is still process-scoped.** Audit JSONL is evidence, not a
  durable transaction/replay database. Restarting the server discards request
  IDs and operation results. A repeated request after restart can execute
  again; inspect the document/output before resubmitting uncertain work.
- **Plans are not atomic.** A failed step stops subsequent calls but preserves
  earlier successful changes. Reported postconditions now stop broken models;
  they do not prove arbitrary design intent. Independent volume/dimension
  comparisons and visual inspection remain necessary.
- **Finite input does not guarantee viable CAD geometry.** The checks prevent
  type/coercion mistakes and documented invalid sizes. They do not establish
  topology, machining feasibility or a safe physical magnitude for every
  finite double. State-dependent checks remain in the native implementation.
- **No-clobber is a pre-execution guard, not an atomic filesystem transaction.**
  The lease coordinates PRO processes. An unrelated application could still
  create/modify a target between the check and COM SaveAs. Multi-file component
  export uses the stricter empty-directory rule because filenames are selected
  by SOLIDWORKS. No generic rollback or backup recovery is claimed.
- **These regressions establish before/after behavior, not CAD accuracy or
  speed.** Use the separately recorded live runs for performance and shape
  evidence. Counts of passing synthetic tests must not be presented as live
  competitor benchmarks.

The follow-up [inertia unit review](measurement-unit-review.md) records a
source-backed unit-label discrepancy in one competitor's three responses.
It is an unscored diagnostic finding. PRO currently does not expose inertia
through its inherited mass tool, which remains a separate capability gap.
