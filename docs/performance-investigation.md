# CAD latency investigation

> Historical report for the releases named below. Version 0.7.0 replaces the former external CAD engine with the [independent native engine](native-engine.md); these findings and timings do not measure that rewrite.

Analysis date: 2026-11-03. This report distinguishes the original recorded benchmark, local Python-only microprofiling, and separately instrumented native diagnostic evidence. The analysis author made no live SOLIDWORKS calls. Native diagnostic data was supplied by the root agent running the sole serialized CAD session. No original result or timing was rewritten.

## What the original 7.9% observation establishes

The original `benchmarks/results/live-after.json` contains three repetitions each of current PRO 0.2.0 and frozen PRO 0.1.0. Seven measured stages are included: new part, block, hole, dimension edit, mass query, STEP export and close. Policy probes and independent oracle reads are excluded from timing totals.

| Repetition | PRO 0.2.0 total ms | Frozen 0.1.0 total ms | Same-index difference ms |
|---|---:|---:|---:|
| 0 | 8,249.341 | 6,937.263 | +1,312.078 |
| 1 | 7,508.313 | 7,993.111 | -484.798 |
| 2 | 6,761.471 | 6,958.441 | -196.970 |
| Median | 7,508.313 | 6,958.441 | +549.872 between medians |
| Mean | 7,506.375 | 7,296.272 | +210.103 between means |
| Sample standard deviation | 743.937 | 603.573 | — |

The median difference is +7.9%; the mean difference is approximately +2.88%. These are different summaries of the same small sample. They do not justify discarding the slower first PRO run or replacing the published median with a preferred statistic. They also do not establish a uniform 7.9% code regression: current PRO was faster in two of the three same-index comparisons. Same-index differences are descriptive, not randomized paired estimates.

The fixed seed happened to produce **PRO → Andrew → frozen PRO** in all three after-run repetitions. Therefore version and order were confounded. PRO total decreased monotonically by roughly 0.74 seconds per repetition. The unchanged frozen baseline's median also fell from 7,913.868 ms in the earlier before-run to 6,958.441 ms in the after-run (about 12.1%). This is evidence of run-to-run variability; cache, application state and background-load explanations remain hypotheses unless separately controlled.

### Where the differences appear

| Stage | Current samples ms (r0/r1/r2) | Frozen samples ms (r0/r1/r2) | Current minus frozen mean ms |
|---|---|---|---:|
| New part | 2250.298 / 1967.462 / 1486.341 | 1540.774 / 1973.984 / 1469.229 | +240.038 |
| Block | 1662.936 / 1495.496 / 1491.072 | 1569.344 / 1608.331 / 1448.197 | +7.879 |
| Hole | 1502.107 / 1506.767 / 1391.956 | 1436.005 / 1333.317 / 1435.326 | +65.394 |
| Edit | 973.930 / 809.891 / 818.167 | 724.333 / 882.340 / 794.505 | +66.937 |
| Mass | 125.073 / 104.869 / 99.344 | 379.727 / 721.238 / 430.069 | -400.583 |
| Export | 588.387 / 475.195 / 515.828 | 457.287 / 632.525 / 530.284 | -13.562 |
| Close | 1146.610 / 1148.633 / 958.763 | 829.793 / 841.376 / 850.831 | +244.002 |

Means are additive; independently computed stage medians are not. The new-part difference is concentrated in repetition 0. The close difference appears in all three repetitions, but the harness runs policy probes immediately before close: frozen PRO performs a native export that overwrites the sentinel, whereas current PRO rejects that export before COM. Those extra uncounted native operations can change the application state before close. Whether they caused the close difference is **not measured**. A cleaner latency comparison should put the policy checks on a separate fresh document, or at least report the differing pre-close context.

## Source analysis of the call path

At the initial 0.2.0 snapshot, native requests travel through:

1. SDK input-schema validation and `ProServer.call` validation/normalization.
2. `OperationRegistry.run`, the in-process execution lock and the cross-process `CadLease`.
3. `NativeBackend.call`: normalized arguments, write collision guard, then the upstream tool wrapper.
4. The upstream COM STA worker, `SolidWorksSession.run_guarded`, the native session method, and a queue-drain sentinel before releasing the lease.
5. Result validation, detached operation result storage, and MCP serialization.

`operations.py` and `storage.py` were identical between frozen 0.1.0 and the reviewed 0.2.0 source. New/close input schemas did not gain geometry constraints. New 0.2.0 normalization and output validation add local Python work, while no-clobber checking applies to output-writing tools, not new/close. The new part and close operations still use the same pinned upstream native implementation. This rules out a newly added file lease or operation registry as an explanation for the version difference; it does not imply that all wrapper overhead is zero.

The existing read-only shortcut for part mass queries avoids the upstream mutation guard, retains the STA worker and lease, checks native results, and keeps the guarded assembly path because assembly bounding-box measurement rebuilds. It explains an intentional architectural difference in the mass-query path. Its measured speedup should not be generalized to arbitrary operations.

The pinned upstream `session.py` guard performs `_reattach_if_gone()`, then `_history_snapshot()`, and only then `_command_in_progress(True)`. The history snapshot tests document type, reads the title and traverses the feature tree through `_history()`, `_require_part()`, `_require_model()` and `_iter_features()`. The upstream comments themselves document the latency benefit of `CommandInProgress`, but that comment is not a measurement of this task. The native profile below supplies the relevant evidence.

### New part and close have different work

`new_part` resolves the default template, calls `NewDocument` or `NewPart`, wraps the model and returns its title. On a fresh server `_sw` is `None`; both the history snapshot and the first `_command_in_progress(True)` return without work. The actual connection occurs later inside `new_part` via `_ensure()`, including type-library wrapper setup, visibility and automation preferences. Merely moving the history snapshot cannot eliminate this first-call connection/document-creation cost.

`close_part` retains existing save/unsaved-document rules, calls `_require_model()`, then `CloseDoc` and clears its model reference. Its generic guard nevertheless snapshots all part history first. `_require_model()` also applies the temporary quiet-UI settings when inside a guard; waking a document that has just closed can encounter a handled COM error. Whether removing redundant lifecycle-specific work is worthwhile requires a separately measured change. The initial recommendation keeps the same checks and rollback behavior.

## Local Python-only overhead measurements

A separate offline microprofile used captured current-PRO schemas, arguments and outputs from the original after-run. No native backend or SOLIDWORKS session was imported or called. Each combined iteration ran SDK-style `jsonschema.validate`, server normalization and schema validation, backend normalization/write guard, then result validation. Export used a non-existing sanitized path; this was not a CAD export.

| Local check path | Samples | Median ms | Min–max ms |
|---|---:|---:|---:|
| New part validation | 25 | 0.5432 | 0.5013–0.9444 |
| Block validation | 25 | 2.5011 | 2.3863–3.1109 |
| Hole validation | 25 | 2.0790 | 1.8514–2.5148 |
| Edit validation | 25 | 1.1431 | 1.0877–1.6084 |
| Mass validation | 25 | 0.5561 | 0.5371–0.8002 |
| Export validation/path checks | 25 | 5.9140 | 5.1991–7.4417 |
| Close validation | 25 | 0.7798 | 0.7506–1.2232 |
| Isolated file lease acquire/release | 100 | 0.2943 | 0.2306–0.7962 |
| Registry with immediate fake result | 100 | 0.0492 | 0.0437–0.2839 |

The lease used a unique temporary lock path, never the live CAD lock. These are local warm measurements, not end-to-end MCP timings, and do not include COM or transport. They show that deleting safety validation to recover hundreds of milliseconds would target the wrong scale. Precompiling validators or avoiding duplicate JSON copies remains a low-priority optimization, contingent on preserving all direct-call and wire validation guarantees.

## Instrumented native profile: measured expensive history snapshots

Diagnostic source: `.local/profile_server.py`; raw phase events: `artifacts/profiling/56b075ea37/phases.json`. These local diagnostic files are not included by the source ZIP packager. The root agent instrumented the unchanged 0.2.0 call path, wrapping methods with `perf_counter` timing. The file has 184 phase events over three diagnostic sequences. Wrappers add measurement overhead, and this diagnostic session's timing distribution differs from the earlier benchmark. **Do not substitute these timings for the final competitor benchmark.** Nested inclusive phase durations must not be summed indiscriminately.

There are 18 guarded calls, comprising six guarded operations per sequence. Part mass queries take the existing fast path, and nested mass measurements appear inside feature operations. The three new-part history snapshots are effectively zero. The other **15 history snapshots** take **449.680–702.675 ms**, with a **631.435 ms median** and approximately **9,210.85 ms total**, or **3,070.28 ms per diagnostic sequence**. The median across all 18 snapshots, including the empty ones, is 590.535 ms.

| Guarded operation | Snapshot samples ms | Guard total samples ms |
|---|---|---|
| New part | 0.001 / 0.005 / 0.006 | 1899.187 / 1141.271 / 1259.706 |
| Block | 596.844 / 449.680 / 576.298 | 2038.193 / 1791.577 / 1913.838 |
| Hole | 642.528 / 650.818 / 552.374 | 1867.285 / 1874.052 / 1730.512 |
| Edit | 678.880 / 694.381 / 555.335 | 1395.617 / 1371.365 / 1236.663 |
| Export | 702.675 / 680.358 / 542.780 | 859.660 / 841.870 / 694.723 |
| Close | 672.243 / 631.435 / 584.227 | 1556.388 / 1539.405 / 1477.333 |

Other inclusive phase medians: `_command_in_progress` 0.773 ms across 36 invocations, `_quiet` 8.428 ms across 42 invocations, `_wake_all` 24.595 ms across 18 invocations. The one connection phase took 117.341 ms. New-part method bodies had a 965.904 ms median and a 861.542–1899.126 ms range; close bodies had an 836.154 ms median and a 827.638–849.738 ms range. The history traversal is therefore a measured substantial overhead shared by the existing guarded paths, including close. It is not proof that the original 0.2.0-versus-0.1.0 difference came from that same unchanged code.

## Recommended change and safety conditions

The selected candidate is to set `CommandInProgress` before the history snapshot while keeping `_quiet_ui` around the actual operation exactly as upstream originally did. This is a sequencing change, not removal of history capture or rollback. An initial trial also widened the quiet scope to include the snapshot; diagnostic results rejected that variant because UI wake cost increased. The root agent supplied these separate instrumented trials:

| Diagnostic phase | Original 0.2.0 (`56b075ea37`) | Busy + widened quiet scope (`ef7c361ad0`) | Busy before snapshot; original quiet scope (`4de03503a0`) |
|---|---:|---:|---:|
| Nonempty snapshot median, n=15 | 631.435 ms | 161.344 ms | 120.804 ms |
| Nonempty snapshot range | 449.680–702.675 ms | 107.467–185.881 ms | 98.089–135.938 ms |
| UI wake median, n=18 | 24.595 ms | 92.816 ms | 24.382 ms |
| UI wake maximum | 50.189 ms | 603.732 ms | 48.844 ms |

The widened scope affects when `_quiet` collects view/tree objects for later restoration. The higher wake latency was measured; the exact renderer/UI scheduling mechanism causing it was not isolated. The narrower change preserves the original UI scope and reduced the snapshot phase substantially in this diagnostic run. These sequential diagnostic samples are not the final comparison against rivals and do not prove an identical end-to-end gain.

The chosen busy-only trial still had a 2,653.369 ms first new-part method versus later calls around 958–980 ms. The final selected implementation adds early attachment **only for the actual bound session `new_part` method when `_sw` is absent**. It does not extend this behavior to `new_assembly` or unrelated methods. The pinned upstream `new_part` begins with `_ensure()` anyway. This preserves the connection action but lets the first `NewDocument` execute with the same command flag as subsequent creations. It remains inside the STA worker and lease.

The final selected diagnostic run `cf68660718` records a 125.106 ms median across 15 nonempty history snapshots, and 24.767 ms UI-wake median across 18 invocations. Its connection phase was 113.331 ms. The method-body timings of new part were 930.869 / 777.131 / 1025.726 ms; these exclude the newly moved connection phase and must not be compared as end-to-end request totals. The separate captured MCP records give the meaningful full call and seven-call sequence totals:

| Diagnostic repetition | Full new-part MCP call ms | Seven-call MCP total ms | Post-edit oracle volume mm³ | Oracle size mm |
|---|---:|---:|---:|---|
| 0 | 1072.527 | 5851.307 | 47057.522203923065 | 80 / 50 / 12 |
| 1 | 1015.271 | 5782.793 | 47057.522203923065 | 80 / 50 / 12 |
| 2 | 1333.779 | 6088.061 | 47057.522203923065 | 80 / 50 / 12 |

These are development diagnostics of the selected code, not the final counterbalanced rival comparison. The root agent reported 10 passing guard tests covering ordering, rollback, pre-existing busy state, snapshot failure, failed initial connection and failed initial document creation. Full repeated correctness/policy/performance comparison is a separate acceptance step.

### Diagnostic provenance

The times below are filesystem last-write UTC observed during this audit, not signed timestamps embedded in the JSON. SHA-256 identifies the exact raw bytes analyzed. All paths are relative to the repository root. The package excludes `artifacts`, so retaining or separately distributing these originals is necessary to independently inspect these local phase claims.

| Raw phase file under `artifacts/profiling` | Observed last-write UTC | Events | SHA-256 |
|---|---|---:|---|
| `56b075ea37/phases.json` | 2026-11-03T10:31:38.593085Z | 184 | `c72d39ca6f6f8a67282ce3a93818bbfa64691dc29a44a03cb572f4c401e0caaf` |
| `ef7c361ad0/phases.json` | 2026-11-03T10:33:19.578303Z | 181 | `871b4d1eadf39d015ef68e1cca6eeb0a609ef411adf2ce3bfca1d713560a0d66` |
| `4de03503a0/phases.json` | 2026-11-03T10:35:04.517350Z | 166 | `6ad55cf482706b91606c98c036034c6a42fb17715b63a64387407013b312e133` |
| `cf68660718/phases.json` | 2026-11-03T10:36:21.285306Z | 166 | `d4c74ef23955374f4ea858a314ee22a31b47ec1a698595ddc37479221381e23f` |

Selected diagnostic MCP call records:

| File under `artifacts/profiling/cf68660718` | SHA-256 |
|---|---|
| `0/calls.json` | `7718a9d0738c9088ba3f1f7408c43083120c59596d4224a28d372d2fc8426708` |
| `1/calls.json` | `af029c14c8fe49fefb1f9bc60544ab5256448cd71783e734f2859933c637f5b0` |
| `2/calls.json` | `6163028cb16ad8fbc4f16e7971ee78abfe51e3e4c9d9f3a4b294ca98246d9552` |

Preserve the following conditions:

- Reattach handling occurs first. Initialize `snapshot = None` before the protected operation so a snapshot exception does not use an uninitialized value.
- Restore the previous command flag even if the snapshot, native method, rollback or UI restoration fails. A flag already set by an outer command must not be cleared by the inner call.
- Preserve the original quiet-UI placement around the method, and exception ordering: `_quiet_ui` exits and restores the UI before the outer exception handler invokes `_roll_back(snapshot)`; the command flag is restored in `finally`.
- A snapshot failure does not trigger deletion without a complete snapshot. New/closed/switched documents retain the upstream title/reference checks that prevent rolling back another document.
- Keep the sketch/view/tree suppression responsible for crash avoidance, feature postcondition validation, no-clobber checks, STA confinement, operation replay policy, and lease lifetime through native completion.
- Verify nested quiet scopes, pre-existing disabled view flags, existing `CommandInProgress=True`, a partial feature failure, an assembly, a document switch and a close. Tests of error paths complement a fresh live correctness benchmark.

Skipping history capture only for new/close is a separate possible optimization: those methods do not add features to the previous document, and a closed document cannot be rolled back. It should not be bundled into the first sequencing experiment without measurement and focused lifecycle tests. Replacing native geometry verification with cached results, removing the queue-drain/cancellation ownership guarantee, or measuring only the fastest repetition would not meet the requested safety and evidence standard.

For the next comparison, freeze the actual 0.2.0 code as `pro_v020`, retain raw old evidence, keep identical dependency versions, and use a counterbalanced order across repetitions. Report policy cases separately with explicit pre-close state. Compare complete valid sequences as well as stage distributions, record all failures and verify returned volume against the same-record oracle. Any claim of being faster than rivals should name the exact scenario, pins, sample count and observed range; there is no basis for a promise of universally faster CAD execution.

## Supporting benchmark change

The returned-volume checker was extended to recognize the explicit `pro_v020` adapter ID using the existing native `mass_properties.volume_mm3` contract. The parameterized contract test now includes that baseline. All 43 measurement-check tests passed; no historical output or timing changed. This is benchmark bookkeeping, not a native performance change.
