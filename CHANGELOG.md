# Changelog

## 0.7.0 — 2026-11-04

- Replace the external CAD MCP runtime and adapted guard with an independent
  88-tool engine: native SDK binding, bounded STA worker, document/session policy,
  geometry, features, assemblies, topology and offline mesh operations.
- Preserve public tool names and units while requiring explicit edit authorization
  for existing documents. Reject silently changed active targets and stale entity IDs.
- Check native features, errors and measurements. Record undo availability, verify
  the observed rollback signature, and report uncertain outcomes explicitly.
- Retain the 35 PRO tools, compact workflow profile, rendering and engineering
  workflows on the independent runtime. Remove the external CAD engine dependency.
- Add separate native acceptance recipes for all 88 tools, with analytic geometry
  expectations, prerequisite failures and owned-document cleanup evidence.
- Verify all 88 distinct tools across a full catalogue run and focused native
  retesting. Publish 1,684 automated tests plus independent engineering, add-in
  rendering and project-shot-pack evidence with source hashes and exact scope.

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
