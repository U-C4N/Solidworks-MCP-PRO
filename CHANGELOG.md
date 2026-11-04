# Changelog

## 0.6.0 — 2026-11-04

- Check explicit design requirements with verified native units, absolute/relative
  tolerances, pass/fail/unknown evidence, tight solid bounds and selection-free
  exact component-pair measurements. Do not rebuild, activate or save during checks.
- Evaluate up to sixteen explicit one/two-dimension combinations on independent
  saved part copies. Verify signed dimension readback, rebuilds, source hashes,
  open-source state, copy cleanup and active-document restoration. Export CSV/JSON
  evidence and rank only valid candidates by native mass.
- Generate 1–12 native product shots with exact-aspect previews, final PNGs,
  custom/standard views, contact sheets and manifests. Retain partial verified
  output and verify original-camera restoration on success/failure/cancellation.
  Opt-in project rendering uses an explicit bounding-sphere-fit camera to avoid
  model-view auto-fit/cropping during portrait conversion; routes never silently
  fall back to one another.
- Identify runtime/package/source and published-tool profile mismatches through
  a read-only connection doctor, with optional redacted client-file inspection.
  Preserve the legacy dependency-only `--doctor` behavior.
- Add explicit add-in and project-renderer acceptance probes with independent
  image verification, scratch ownership, route-specific results and cleanup.
  Native add-in preview/final rendering succeeded in a new 2026 SP4.1 session;
  this does not establish the cause of the earlier session's macro failure.
- Publish 35 PRO tools (123 total) in full and 14 workflows in compact, retaining
  native plan access and on-demand schemas. Include the existing assembly,
  named-camera and geometric-bending bridges in the current documentation.
- Refresh the README, usage examples, native acceptance script and versioned
  validation evidence. Automated and real CAD/render results remain distinct.

## 0.5.0 — 2026-11-03

- Diagnose exact active saved models, with optional native geometry/interference checks. Plan repairs without changes; apply separately with stale-state and replay checks.
- Recognize reported mate errors, failing features and broken equations as failed postconditions while preserving warning-only evidence.
- Batch RGB with complete preflight, native readback, no-op skipping, one redraw and verified rollback, including previously absent overrides.
- Orchestrate static normal/exploded preview and final renders under one operation/lease. Restore exact matrices and model-view camera without saving or rebuilding posed models.
- Offer a ten-tool compact profile, on-demand tool schemas, concise results backed by full evidence, image-free workflow polling and explicit image retrieval. Existing launches default to full; generated snippets select compact.
- Provide separately labelled discovery/envelope benchmarks and opt-in native scratch acceptance. No universal speed or token percentage is claimed; native RGB readback does not establish P2M render fidelity.

## 0.4.0 — 2026-11-03

- Provide 17 PRO tools (105 total), adding native Visualize project launch, component appearance assignment, scene/HDRI/light controls and active-view camera framing.
- Run native rendering and 2026 grouped project export through fixed internal SOLIDWORKS macros to preserve the UI apartment required by material conversion. No arbitrary macro input or Computer Use is exposed.
- Validate exact active saved CAD paths, appearance targets, native light IDs, camera vectors and post-application readbacks. Keep partial-state and unsupported-setting failures explicit; no automatic scene or camera save is implied.
- Retain decoded image dimensions, output hashes, stage timing and background-operation tracking. Verify real 4K Visualize output, native camera framing, HDRI assignment and light disabling separately from the historical CAD speed benchmark.
- Document the observed RealView prerequisite for scene preset insertion and native floor-setting limitations. Validate the integrated source with 561 tests and Ruff; build local wheel and source ZIP artifacts.

## 0.3.0 — 2026-11-03

- Enter SOLIDWORKS command mode before capturing feature history. Preserve the complete snapshot, exception rollback, original quiet-UI scope and prior command flag restoration.
- Attach before the command-mode guard on the first `new_part`, so its `NewDocument` runs in command mode too. Other tools do not gain eager connection side effects.
- Compile and check MCP tool schemas once at initialization, while normalizing and validating every request through the same PRO validator. Remove the SDK's duplicate per-call schema meta-validation.
- Benchmark current PRO, frozen 0.2.0 and four unchanged rivals with retained warm-ups and six counterbalanced repetitions. Close the measured CAD fixture before running default-policy probes on a separate fixture.
- Retain historical measurements; publish source digests, independent volume/bounds checks, per-stage distributions and scoped speed comparisons.

## 0.2.0 — 2026-11-03

- Benchmark unmodified, pinned hjbaard, HarrierPigeon, soheil-mp and Andrew implementations through MCP stdio; serial native CAD cases use a separate COM volume/bounds oracle. Keep discovery, geometry, default safety policies and speed measurements separate.
- Reject malformed nested geometry, nonfinite/overflowing numbers and documented invalid size relationships before CAD. Validate all plan inputs and output targets before any step runs.
- Stop plans when native responses report a failed rebuild or broken features even if their `ok` flag is true.
- Refuse existing native save/export/screenshot/drawing output by default; explicit `overwrite:true` allows intentional replacement. Account for added native suffixes and per-component export collisions.
- Avoid feature-history snapshots and UI redraw work for part mass-property reads; keep the original guarded path for assemblies and all modelling mutations.
- Publish raw benchmark evidence, repeated descriptive statistics, new regression tests and remaining limitations. No universal speed or capability supremacy is claimed.

## 0.1.0 — 2026-11-03

- Extend `solidworks-mcp==0.19.0` with 13 PRO tools (101 total).
- Official SOLIDWORKS Visualize add-in: detect/load, photorealistic PNG/JPEG rendering, native SVPJ export, restored settings and decoded output validation.
- Typed Visualize COM vtable calls on the existing STA worker; avoids the live-tested pywin32 IDispatch return-interface failure.
- Background operations, polling, request deduplication and cross-process PRO lease.
- Complete-plan schema validation, sequential execution, failure stop and plan audit.
- Inline model screenshots, expected-volume comparison and offline STL viewer/SVG/topology inspection.
- Codex/Claude configuration generator, Windows setup, protocol smoke tests and source/wheel packaging.

See README.md for current capabilities and tested limits; versioned validation files retain historical evidence. There is no blanket superiority or all-version compatibility claim.
