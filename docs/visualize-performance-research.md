# Visualize 4K quality and performance: verified controls and useful additions

Research date: 2026-11-03. This investigation read source files, an existing
generated type-library binding and official help pages. It did not attach to
SOLIDWORKS, call COM, launch Visualize or render a model. The controlling agent
owns the live `Kollu_Pres_3D` render and its measurements. Recommendations below
are not claims that this particular scene has already passed visual acceptance.

## What the current native bridge can control

The inspected implementation is `src/solidworks_mcp_pro/visualize_bridge.py`.
It renders the active native CAD document through
`IVisualizeAddinManager.Render()`. Its current arguments select image size,
pass target, denoiser and PNG/JPEG. It sets `TerminationMode=QualityLevel` and
`IncludeAlpha=False`. It snapshots changed settings, checks each write, restores
them on success or failure, and validates newly created images by full decoding,
dimensions, format and SHA-256. Keep these protections when adding controls.

The installed Visualize interface metadata has exactly these `IRenderOptions`
properties:

| Property | Native type / purpose | Current bridge |
|---|---|---|
| `JobName`, `OutputFolder` | String; job name and destination | Set per isolated job |
| `ImageFormat` | Enum: JPEG 0, BMP 1, TIFF 2, PNG 3 | PNG/JPEG supported |
| `IncludeAlpha` | Boolean | Set false |
| `Width`, `Height` | Signed 32-bit integer; pixels | Exposed |
| `DenoiserEnabled` | Boolean | Exposed |
| `TerminationMode` | Enum: TimeLimit 0, QualityLevel 1 | QualityLevel |
| `FrameCount` | Signed 32-bit integer | Exposed as `passes` |
| `Milliseconds` | Signed 64-bit integer | Not yet exposed in inspected source |

There is no GPU selector, render-engine selector, camera, appearance or HDRI
property in this interface. The metadata also exposes the 2026 manager's
`SaveWithPartGrouping` and `LaunchVisualize` methods. Export/open is a grounded
handoff to Visualize scene editing; it does not establish an API for changing
the scene inside Visualize.
[Official manager members](https://help.solidworks.com/2026/english/api/swvisualizeapi/SolidWorks.Visualize.Interfaces~SolidWorks.Visualize.Interfaces.IVisualizeAddinManager_members.html),
[official render example](https://help.solidworks.com/2026/english/api/swvisualizeapi/SolidWorks.Visualize.Interfaces~SolidWorks.Visualize.Interfaces.IVisualizeAddinManager.html).

Metadata evidence, read without importing or generating COM bindings:

- Installed typelib: `C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS Visualize\SolidWorks.Visualize.Interfaces.tlb`.
  SHA-256: `4e62040b2a19b2c4b04b3c987d6b92a115ce19ae73a15e9fdfb1594d3b97dd08`.
- Existing generated source: `.venv/Lib/site-packages/comtypes/gen/_2615126C_0ACD_4C09_A4ED_A31DC6A93B14_0_1_0.py`.
  SHA-256: `377af4e4b88b7321c35f947e8e939c77922aaf9cd8cc0e9c12966ce672e61786`.
- Type library GUID `{2615126C-0ACD-4C09-A4ED-A31DC6A93B14}`, version 1.0.
  This version is the COM library version, not the product release number.
- `IRenderOptions` declarations are at generated lines 388–423 and 545–665;
  manager2 methods at 742–762. These generated bindings are local evidence and
  are not packaged as a portable replacement for the installed Visualize API.

## A defensible 4K rendering workflow

Use native Visualize output at **3840 × 2160**, which is 8,294,400 pixels and
four times the pixel count of 1920 × 1080. This is a pixel-count ratio, not a
promise of four times the render duration. Scene transfer, initialization,
denoising, memory use and file writing contribute separately.

For `Kollu_Pres_3D`, the proposed acceptance criteria are: the entire intended
press is in frame, the handle and working surfaces remain readable, the image
has plausible material/reflection differences, and no fine feature is visibly
erased or smeared. This is a proposed visual review checklist, not a statement
about the unseen model's materials or construction. Review both the full frame
and 100% crops of small details, contact shadows and reflective edges. Image
validity alone cannot establish photorealism.

First render a lower-resolution composition check. Then compare a few fixed
quality targets, including denoiser on and off, with the same camera and scene.
Choose the lowest measured cost that meets the visual brief, then produce the
native 4K final and inspect its detail crops again. Preserve CAD identity and
geometry throughout; image generation or upscaling is not evidence of a native
4K CAD render.

Visualize's 2026 documentation distinguishes Preview rasterization, Fast
raytracing and Accurate pathtracing. Its denoiser guidance reduces the pass
target to obtain a time-to-quality benefit. Keeping the same pass target and
turning denoising on does not establish a speed improvement. Treat the vendor's
suggested pass reductions as starting hypotheses, not measured performance for
this scene. Denoising can introduce unwanted artifacts.
[Render Tab, 2026](https://help.solidworks.com/2026/english/visualize/r_render_tab.htm?id=7.3.0.8),
[DenoiserEnabled API, 2026](https://help.solidworks.com/2026/english/api/swvisualizeapi/SolidWorks.Visualize.Interfaces~SolidWorks.Visualize.Interfaces.IRenderOptions~DenoiserEnabled.html).

## Camera, lighting, materials and GPU: distinguish UI from API

Visualize 2026 documents creating/loading/saving cameras, adding an HDR
environment, changing its brightness, and creating appearances. Those are
documented scene-editing features in the application UI. They are not arguments
to the inspected add-in rendering API. A part-grouped SVPJ is useful when
individual components need different appearances. Reopen and inspect the
project to verify the scene survives the handoff.
[Project menu, 2026](https://help.solidworks.com/2026/English/Visualize/r_Visualize_project_menu.htm?id=0f3a46798de94e0eaeec7c099a3242a1),
[grouped export API, 2026](https://help.solidworks.com/2026/english/api/swvisualizeapi/SolidWorks.Visualize.Interfaces~SolidWorks.Visualize.Interfaces.IVisualizeAddinManager2~SaveWithPartGrouping.html).

For a product image, a stable camera and controlled HDR lighting are sensible
starting choices. Avoid adding strong depth-of-field blur before the mechanical
details have been reviewed. Appearance roughness and texture scale require
inspection on the actual geometry. These are design recommendations, not
automatic settings in PRO. The retrieved appearance-editing documentation is
explicitly **2025**; it describes material types and editable roughness, albedo
and transparency. It should not be cited as proof of a new 2026 API.
[Creating and editing appearances, 2025](https://help.solidworks.com/2025/english/Visualize/t_create_edit_appearances.htm).

The 2026 Render Tab documents CPU/GPU/Hybrid device choices in Visualize's UI.
The retrieved **2025** 3D Viewport options page additionally describes engine
selection, compatible GPU lists, live/background operation and tiling. It says
changing engines requires restarting Visualize and can change the result;
tiling can trade memory for speed and introduce boundary artifacts. Do not
silently modify those application settings during a benchmark or claim an
installed GPU was used merely because it exists. Record the actually confirmed
engine/device/mode, or explicitly mark them unknown. The add-in's ten render
options cannot confirm them.
[Render device UI, 2026](https://help.solidworks.com/2026/english/visualize/r_render_tab.htm?id=7.3.0.8),
[3D Viewport options, 2025](https://help.solidworks.com/2025/English/Visualize/r_Visualize_Options_db_3D_Viewport_Tab.htm?id=02e129db8b3946318617f873570eaa52).

## Five measurable MCP improvements

| Priority / feature | Grounded implementation | Useful measurement / acceptance |
|---|---|---|
| 1. Phase timing and render manifest | Time manager setup/options, native synchronous `Render`, image validation and restoration separately using a monotonic clock. Store request values, source identity, API revision, image dimensions/hash and observed engine/device if available. | Native-call duration and complete job duration both reported; phase sum reconciles within timer overhead. No claim that the native call is pure raytracing time. |
| 2. Quality comparison tool | Serial jobs using existing `Width`, `Height`, `FrameCount`, `DenoiserEnabled`; preserve one source scene and produce full-frame/crop comparisons plus raw JSON. | Same scene across all runs, all output dimensions verified, quality decision explicit. Report measured time-to-accepted-quality rather than same-pass timing as the denoiser's benefit. |
| 3. Native time budget | Expose mutually clear quality/time modes; map duration to `TerminationMode=0` and 64-bit `Milliseconds`. Snapshot, read back and restore both relevant options. | Verify native output and actual wall time for a short budget. Report startup/output overhead separately. A native render budget is not a hard deadline for the complete MCP operation. |
| 4. Actual progress and controlled cancellation | Implement and validate `StartRender2` plus notification/callback interfaces on the existing STA; keep the writer lease until a terminal native state. | Report actual pass/progress/status events and cancellation-to-terminal latency. Never synthesize percentages or release the lease just because the client disconnected. |
| 5. Transparent PNG output | Expose `IncludeAlpha` only with a supported format; retain isolated outputs and option restoration. | Verify PNG mode/channel and inspect alpha extrema. A valid alpha channel may be fully opaque, so distinguish channel presence from useful transparency. |

The first two features can improve observability and selection immediately
without pretending to control undocumented GPU or scene settings. The native
time option is supported by local metadata, but its exact termination behavior
must be tested before promising a deadline. Camera/HDR/material automation is a
larger separate integration task: first establish a documented control route
and scene fidelity, then add MCP arguments.

### Async interop details that require a real test

`IVisualizeAddinManager2.StartRender2` takes a **VARIANT** handler in the installed
type library and returns a Boolean. `IRenderNotificationHandler2` exposes
`RendererPropertyChangedNotify` and `SetCallbackHandler`. The callback exposes
`OnPreviewRequested(width, height)` and `OnRenderActionRequested(action)`;
actions are Continue 0, Cancel 1 and CancelAndSave 2.

Do not assume inheritance from similar names: the generated
`IRendererPropertiesStatus2` exposes only `BitmapPreviewImageSource2`, while
`CurrentProgress`, `FramesRendered`, `ElapsedTime` and `RenderJobStatus` are on
`IRendererPropertiesStatus`. Verify QueryInterface on real callback objects.
`ElapsedTime` is a marshalled .NET `TimeSpan`, not a plain millisecond number.
`IBitmapImageSource.GetPixels` returns bytes, but pixel layout/stride are not
established by these declarations. Do not guess their format.

Keep callbacks alive, respect STA/message-pump requirements, distinguish render
completion from output completion, and confirm terminal cancellation before
restoring options or releasing the CAD lease. This research establishes method
metadata, not working async callback behavior. The current synchronous route
remains the verified rendering route until that separate acceptance succeeds.

## Reproducible performance experiment

Record raw runs rather than only an average: source document/references,
configuration, camera/scene identity when available, settings, dimensions,
software version, confirmed device/mode, start/end time, native-call duration,
total duration, image hash and result. Keep unknowns explicit.

Measure cold initialization separately. For each candidate preset, retain a
warmup and run at least three measured samples in alternating order where
practical. Serialize all CAD/render work, avoid UI changes during each sample,
and record other relevant system load. Compare medians and range; a single
4K run is a useful delivery measurement but cannot establish a reliable speedup.

For quality review, use the same full frame and fixed detail crop positions.
An optional higher-pass reference can support image-difference metrics, but
PSNR/SSIM alone cannot establish material accuracy or engineering fidelity.
Do not combine native raytracing performance with the unrelated CAD-operation
benchmark, and do not infer faster rendering than competitors without equivalent
native-render measurements from those competitors.

Some direct 2026 API URLs returned the Web Help navigation shell or an access
error during research. Search-indexed official pages supplied the manager and
denoiser documentation; exact additional signatures above are grounded in the
hashed installed typelib binding. UI pages retrieved only for earlier versions
are labelled by their actual version.

## Native appearance repair without computer use

Follow-up source investigation on the same date: an all-white Visualize render
can justify checking native render appearances separately from legacy RGB
display properties. This is a diagnostic hypothesis, not proof of the cause of
the reported color loss. Creating a native appearance does not change the
engineering material used for mass or density.

The installed SW2026 pywin32 wrapper at
`C:\Users\ASUS\AppData\Local\Temp\gen_py\3.12\83A33D31-27C5-11CE-BFD4-00400513BB57x0x34x0.py`
confirms these method signatures by source inspection:

```python
appearance = model.Extension.CreateRenderMaterial(full_p2m_path)
accepted_entity = appearance.AddEntity(component)  # check returned bool
result = model.Extension.AddDisplayStateSpecificRenderMaterial(
    appearance, 1, None  # swThisDisplayState; out IDs omitted by makepy wrapper
)
# Preserve and inspect bool + both output IDs returned by the generated wrapper.
# Read back appearance.GetEntities()/GetEntitiesCount() and material counts.
```

This is a candidate component-level recipe, not code executed by this research
agent. `AddEntity` accepts a dispatch object and its official page describes a
generic entity; the retrieved page does not enumerate accepted entity classes.
Component acceptance and the Visualize export result therefore require live
verification by the sole CAD controller. Do not silently fall back to mutating
referenced part files when an assembly-level application fails.
[AddEntity API](https://help.solidworks.com/2022/English/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.IRenderMaterial~AddEntity.html),
[official appearance creation example, 2026](https://help.solidworks.com/2026/english/api/sldworksapi/Get_Appearance_Filename_Example_VBNET.htm),
[display-state appearance API](https://help.solidworks.com/2023/english/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.IModelDocExtension~AddDisplayStateSpecificRenderMaterial.html).

Display-state options are current 1, all 2, specified 3. Use the scope the user
intends. For a named display state use option 3 and its names, rather than
accidentally applying styling to every display state. The wrapper also exposes
`PrimaryColor` as an integer property; use Windows RGB encoding, not an assumed
HTML hex ordering, and check the readback. Preserve a file's supplied material
parameters unless a specific change is intended.
[display-state enum](https://help.solidworks.com/2025/English/api/swconst/SolidWorks.Interop.swconst~SolidWorks.Interop.swconst.swDisplayStateOpts_e.html),
[PrimaryColor API](https://help.solidworks.com/2025/english/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.IRenderMaterial~PrimaryColor.html),
[Windows RGB](https://learn.microsoft.com/en-us/windows/win32/api/wingdi/nf-wingdi-rgb).

These existing local assets were verified by filesystem enumeration under
`C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\data\graphics\Materials`:

- `painted\car\gloss blue.p2m`
- `painted\car\gloss red.p2m`
- `metal\steel\satin finish stainless steel.p2m`
- `metal\steel\machined steel.p2m`
- `rubber\matte\matte rubber.p2m`
- `plastic\low gloss\blue low gloss plastic.p2m`

No asset was applied by this research agent. Asset selection must match the
intended component finish and must not be presented as preserving original
colors if it actually replaces them.

`model.Extension.InsertScene(full_p2s_path)` returns Boolean. Locally present
scene files include `data\graphics\Scenes\Studio\Soft Box Warm and Cool.p2s`
and `data\graphics\Scenes\Studio\Plain White.p2s` below the SOLIDWORKS install
directory. The official 2026 method requires RealView graphics and calls for a
graphics redraw. Verify `ViewDisplayRealView` support and actual scene behavior;
do not count an unsupported scene operation as success.
[InsertScene API, 2026](https://help.solidworks.com/2026/english/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.IModelDocExtension~InsertScene.html).

For this route, completion evidence should include accepted appearance IDs,
entity/readback checks, native project export, a small rendered color check,
and finally the requested 4K output. A native API accepting an appearance is
not proof that Visualize imported its appearance correctly.
