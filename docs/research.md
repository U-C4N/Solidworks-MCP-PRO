# SolidWorks MCP research and Visualize API evidence

> Historical report for the releases named below. Version 0.7.0 replaces the former external CAD engine with the [independent native engine](native-engine.md); these findings and timings do not measure that rewrite.

Checked on **3 November 2026**. Repository/release times below are UTC; add three
hours for Istanbul. This is a dated, non-exhaustive comparison, not a claim that
one project is universally newest or best. Exact hashes, API URLs and the PyPI
artifact digest are preserved in [research-snapshot.json](research-snapshot.json).

## Baseline decision

Use **`solidworks-mcp==0.19.0` by hjbaard** as a pinned CAD dependency. The live
GitHub API and PyPI both reported this version, released on 3 November at
06:44:14 UTC (09:44 Istanbul). Search-engine copies still showed 0.17.0, so they
were not used to choose the version. The package was uploaded at 06:45:28 UTC.
The release adds edge-relative dimensions for holes and bosses. Its 88-tool
surface is deliberately smaller than some competitors. [Release](https://github.com/hjbaard/SolidWorks-MCP/releases/tag/v0.19.0),
[PyPI metadata](https://pypi.org/pypi/solidworks-mcp/0.19.0/json).

The selection favors a published package, MIT licensing, a dedicated COM STA
worker, measurable parametric results and a manageable extension surface. The
README reports 733 tests and native SOLIDWORKS 2026 exercise; those are upstream
claims, not tests performed by this project. Python requirements are >=3.11;
dependencies include MCP >=1.26,<2 and Windows-only pywin32 >=306.
[Upstream source](https://github.com/hjbaard/SolidWorks-MCP/tree/ddcefc0cfa056002ff8eb8bdb186818ad52dc2f1),
[package description](https://pypi.org/project/solidworks-mcp/0.19.0/).

## Freshness and alternatives

Dates are from each repository's GitHub API. "No release" means the
`releases/latest` endpoint returned 404; it does not mean there are no tags.
Tool counts describe different abstractions and cannot establish quality.

| Repository | Latest published GitHub release | Latest main commit | License / scope |
|---|---|---|---|
| [hjbaard/SolidWorks-MCP](https://github.com/hjbaard/SolidWorks-MCP) | v0.19.0, 2026-11-03 06:44:14 | `ddcefc0`, 2026-11-03 06:44:07 | MIT; selected packaged parametric CAD engine |
| [HarrierPigeon/Solidworks-MCP-Server](https://github.com/HarrierPigeon/Solidworks-MCP-Server) | v0.4.0, 2026-08-07 07:45:40 | `60c11fe`, 2026-08-07 07:44:34 | MIT; broad CAD tools and client installer |
| [czuryk/SolidworksMCP](https://github.com/czuryk/SolidworksMCP) | No release | `eccb8af`, 2026-10-22 09:57:58 | MIT; shape programs, transactions, image comparison |
| [aryesil/SolidworksMCP](https://github.com/aryesil/SolidworksMCP) | No release | `292b4b1`, 2026-08-22 14:18:27 | MIT text with additional attribution; 143-tool claim |
| [AvishakeAdhikary/solidworks-mcp-server](https://github.com/AvishakeAdhikary/solidworks-mcp-server) | No release | `b730c29`, 2026-08-05 12:16:37 | MIT; 41-tool COM implementation |
| [alisamsam/Solidworks-MCP](https://github.com/alisamsam/Solidworks-MCP) | No release | `ee8f42a`, 2026-03-23 11:46:46 | MIT; earlier lineage |
| [andrewbartels1/SolidworksMCP-python](https://github.com/andrewbartels1/SolidworksMCP-python) | v1.5.0, 2026-11-02 02:44:37 | `126beed`, 2026-11-03 05:52:57 | MIT; 154-tool claim, Python 3.13+ |
| [soheil-mp/solidworks-mcp](https://github.com/soheil-mp/solidworks-mcp) | No release | `79fb529`, 2026-10-31 22:01:10 | MIT; 161-tool claim, C#/.NET 10 |
| [wudahua/Solidworks_AssetMate_MechDog](https://github.com/wudahua/Solidworks_AssetMate_MechDog) | v0.8.104, 2026-11-03 08:29:53 | `8b43ecf`, 2026-11-03 08:29:33 | MIT; assembly appearance and drawing/audit workflow |
| [27painter/dsh-sw-cad](https://github.com/27painter/dsh-sw-cad) | No release | `5070673`, 2026-11-03 08:18:15 | MIT; configuration/integration layer |
| [emodarklord/datum-mcp](https://github.com/emodarklord/datum-mcp) | No release | `abb4465`, 2026-11-03 05:20:45 | MIT; parts-focused new server |
| [wzyn20051216/solidworks-automation-skill](https://github.com/wzyn20051216/solidworks-automation-skill) | v2.0.0, 2026-10-05 15:53:44 | `637846a`, 2026-11-01 12:38:28 | MIT; skill, MCP toolkit and execution core |

The most recent release among these inspected projects is AssetMate's
v0.8.104, later on the same day than hjbaard 0.19.0. It targets a different
workflow. Freshness is recorded separately from baseline suitability.
[AssetMate release](https://github.com/wudahua/Solidworks_AssetMate_MechDog/releases/tag/v0.8.104).

HarrierPigeon documents sketches, features, assemblies, stable document-scoped
IDs, model feedback and Codex/Claude setup. Its stated native test version is
SOLIDWORKS 2025. Its latest release adds imports/exports and design-tree
inspection. [README](https://github.com/HarrierPigeon/Solidworks-MCP-Server/blob/60c11fe45b6cfc60c4e2da8ad41fc725a48bf8ea/README.md),
[release](https://github.com/HarrierPigeon/Solidworks-MCP-Server/releases/tag/v0.4.0).

The previously indexed **6.5.31** description for czuryk is stale. Current
validation identifies **6.30.0rc1**, not yet promoted to a stable release. It
documents qualification on SOLIDWORKS 2026 SP2.1 with bounded large programs,
export measurement, edit/revert and explicit limitations. This is a serious
alternative for shape-program and reference-image workflows; its broader
architecture is outside this extension's initial scope.
[Current validation](https://github.com/czuryk/SolidworksMCP/blob/eccb8af4aa76416baf2a5ca99b256da19b73d9b6/docs/validation.md),
[changelog](https://github.com/czuryk/SolidworksMCP/blob/eccb8af4aa76416baf2a5ca99b256da19b73d9b6/CHANGELOG.md).

The aryesil repository still describes 6.5.31 and says it expands an earlier
server from 82 to 143 tools. GitHub's license classifier returns NOASSERTION,
but direct inspection shows the MIT grant, both authors' notices and separate
terms for third-party model weights. No code or weights were copied into PRO.
[README](https://github.com/aryesil/SolidworksMCP/blob/292b4b18d1765705234fefb8e4e92c2ca439af52/README.md),
[license](https://github.com/aryesil/SolidworksMCP/blob/292b4b18d1765705234fefb8e4e92c2ca439af52/LICENSE).

andrewbartels1 documents a wider tool set, detailed live replay tests and an
optional agent layer. soheil-mp documents typed C# calls, document ownership,
rollback, modal-dialog detection and simulation, with SOLIDWORKS 2025 as its
tested target. Both deserve direct benchmark comparison before asserting
superiority. [Python project](https://github.com/andrewbartels1/SolidworksMCP-python/blob/126beed0e7a13214ebdd5724929f4c095cf2642f/README.md),
[C# project](https://github.com/soheil-mp/solidworks-mcp/blob/79fb529b67ca4a0587e5570024a772fad0b2c14d/README.md).

27painter explicitly wires existing servers and skills together, including
U-C4N/Autocad-MCP and wzyn's SolidWorks toolkit. Its newer repository timestamp
does not identify a newer SolidWorks modeling engine. Datum is parts-only and
describes assemblies/drawings as future work. AssetMate emphasizes native BOMs,
annotation audits and drawing-delivery checks.
[Integration README](https://github.com/27painter/dsh-sw-cad/blob/507067326f62d74989b1bf5a6a502ce8f281e606/README.md),
[Datum README](https://github.com/emodarklord/datum-mcp/blob/abb4465520549145ab77f3ae24a9b360e8ebf31d/README.md),
[AssetMate README](https://github.com/wudahua/Solidworks_AssetMate_MechDog/blob/8b43ecfd6d9c20d87a49b5e41ab72da46410a081/README.md).

## What a defensible improvement means

PRO extends the pinned engine with observable operations, controlled artifact
output, client-ready configuration and actual SOLIDWORKS Visualize rendering.
It does not claim more native CAD tools than all alternatives, nor that offline
tests prove CAD correctness. Evaluate improvements with:

1. Real MCP initialization, tools/resources discovery and error propagation.
2. An identical part recipe on the baseline and PRO, measuring native volume,
   dimensions, feature tree and exported formats.
3. Concurrent requests and cancellation, proving that one CAD writer remains.
4. A Visualize render whose decoded image, dimensions and hash match the request.
5. A native SVPJ export reopened in Visualize for scene/material acceptance.

## SOLIDWORKS Visualize: verified automation contract

The user means **SOLIDWORKS Visualize photorealistic rendering**. An HTML/STL
viewer is a separate optional inspection facility and does not fulfill that
rendering requirement.

SOLIDWORKS introduced the Visualize Add-in API in 2025. The official API
accesses the Visualize add-in inside a running SOLIDWORKS session. It supports
rendering the active document and exporting a native Visualize project.
[2025 announcement](https://help.solidworks.com/2025/english/WhatsNew/c_wn2025_viz_api.htm),
[official example](https://help.solidworks.com/2026/english/api/swvisualizeapi/SolidWorks.Visualize.Interfaces~SolidWorks.Visualize.Interfaces.IVisualizeAddinManager.html).

The implementation uses these documented API members:

| Action | Contract |
|---|---|
| Access add-in | `swApp.GetAddInObject("SolidWorks.Visualize.Implementation.VisualizeAddin")` |
| Access renderer | `IVisualizeAddin.GetAddinManager()` |
| Set options | `IVisualizeAddinManager.RenderOptions` -> `IRenderOptions` |
| Synchronous render | `IVisualizeAddinManager.Render()` -> boolean |
| Export on 2025 | `IVisualizeAddinManager.Save(filename)` |
| Export with grouping on 2026+ | `IVisualizeAddin2.GetAddinManager2()` -> `SaveWithPartGrouping(filename, grouping)` |

Render options are `ImageFormat`, `FrameCount`, `Width`, `Height`, `JobName`,
`OutputFolder`, `DenoiserEnabled`, `TerminationMode` and `IncludeAlpha`.
The implemented format values are JPEG=0 and PNG=3. Frame-count termination is
`QualityLevel=1`; the alternative `TimeLimit=0` uses `Milliseconds`, but this
implementation uses frame count. Grouping values are appearance=0, part=1,
none=2. [Image format enum](https://help.solidworks.com/2025/english/api/swvisualizeapi/SolidWorks.Visualize.Interfaces~SolidWorks.Visualize.Interfaces.ImageFormat_e.html),
[termination enum](https://help.solidworks.com/2025/english/api/swvisualizeapi/SolidWorks.Visualize.Interfaces~SolidWorks.Visualize.Interfaces.TerminationMode_e.html),
[grouping enum](https://help.solidworks.com/2026/english/api/swvisualizeapi/SolidWorks.Visualize.Interfaces~SolidWorks.Visualize.Interfaces.PartGroupingMode_e.html),
[project export](https://help.solidworks.com/2026/english/api/swvisualizeapi/SolidWorks.Visualize.Interfaces~SolidWorks.Visualize.Interfaces.IVisualizeAddinManager2~SaveWithPartGrouping.html).

The installed `SolidWorks.Visualize.Interfaces.tlb` was inspected without
starting a render. Its library GUID is
`{2615126C-0ACD-4C09-A4ED-A31DC6A93B14}`, version 1.0. It confirms all of the
above member names, signatures and enum values. pywin32 successfully generated
the typed wrappers. Manager2 is a separate interface, so PRO explicitly queries
the correct interface instead of assuming it inherits manager1's render members.
Subsequent read-only live probing on SOLIDWORKS **2026 SP4.1 (34.4.1)** found
that pywin32's `IDispatch.Invoke` getter returned `E_NOINTERFACE`, even though
querying both add-in interfaces succeeded. External typed .NET calls succeeded,
establishing that this was a dispatch-path issue rather than a requirement to
run inside SOLIDWORKS. The production adapter now uses **comtypes 1.4.17 typed
vtable calls** on the same STA. Live `GetAddinManager`, `GetAddinManager2` and
`RenderOptions` reads succeeded with that path. It also verifies the ROT
connection's process ID against the CAD session before accessing the add-in.
Those probes establish API reachability; successful image rendering is a
separate acceptance check.

That subsequent acceptance check also passed: native Visualize produced a
decoded 640×360 PNG and a non-empty part-grouped SVPJ through the MCP server.
See [validation.md](validation.md) for evidence and the tested scope.

The bridge runs on the same STA worker as CAD. It restores render settings,
refuses existing output files, rejects a false render result and validates
output image decoding/dimensions before success. A client timeout cannot cancel
a native COM render; the operation must keep its writer lease until completion.
The available scene/material/camera setup comes from the active model.

No undocumented renderer command-line switches or private queue protocol are
used. Official pages called "Visualize Command Line Properties" describe MSI
installation options, not rendering switches. Queue rendering is a Visualize
Professional UI feature; its localhost port does not establish a supported
public API. [Installation properties](https://help.solidworks.com/2025/English/Installation/install_guide/r_visualize_command_line_props.htm),
[queue settings](https://help.solidworks.com/2026/english/Visualize/r_queue_options_dialog_box.htm).

## Reproducibility and attribution

The selected release commit is
`ddcefc0cfa056002ff8eb8bdb186818ad52dc2f1`. Its published wheel SHA-256 is
`acc531e4c5d6f95ade86ee7d3699b3989f11dc7b4437e34b240de9ca4dff2571`.
The pinned package remains an attributed dependency, not newly authored PRO
CAD code. Preserve upstream license notices when redistributing it. This
research comparison fetched metadata, documentation and installed type information.
The selected hjbaard dependency was then exercised through PRO's live acceptance
test. Subsequent v0.2.0 work ran all four pinned competitors through actual native CAD benchmarks; see [measured results](benchmark-results.md) and [competitor setup](competitor-review.md). The initial research above is source review, while the linked reports contain measured evidence.
