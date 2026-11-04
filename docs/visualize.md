# Photorealistic rendering with SOLIDWORKS Visualize

PRO connects to the **SOLIDWORKS Visualize add-in** through Dassault Systèmes'
documented COM API. `pro_visualize_render` creates a ray-traced image;
`pro_preview_model` and `pro_preview_stl` are separate mesh-inspection tools.
The mesh viewer does not replace Visualize.

## Requirements

- Windows, a running licensed SOLIDWORKS **2025 or newer**, and the corresponding
  installed/licensed SOLIDWORKS Visualize product.
- Enable **SOLIDWORKS Visualize** in SOLIDWORKS **Tools > Add-ins**.
- A native part or assembly selected as the MCP session's target document.
- The installed Visualize interface type library, pywin32 and comtypes 1.4.17.
  PRO uses typed vtable calls for Visualize setup on the same CAD STA thread. The
  interface-returning getters failed through pywin32's dispatch route on the
  tested SW2026 SP4.1 installation, but native typed calls work.
- SOLIDWORKS **2026 or newer** for explicit part/appearance/no-grouping SVPJ
  export. SOLIDWORKS 2025 supports the older export call with its default grouping.

On the tested installation, native rendering and 2026 grouped export execute a
fixed internal macro through `RunMacro2` on the SOLIDWORKS UI thread. This avoids
an observed material-conversion WPF/OLE failure in the external .NET call path.
The macro exposes no caller-supplied code and requires no Computer Use. Options,
completion markers and output files remain verified by PRO.

The official example requires the add-in to be enabled before requesting its
manager. By default, `pro_visualize_status` probes the existing object. Passing
`{"load_if_missing": true}` explicitly requests loading the installed Visualize
add-in when absent. That route reads the fixed Visualize registry registration,
accepts only the known local add-in DLL and verifies API availability after
`LoadAddIn`. It never takes a caller-supplied DLL path, changes startup
registration or starts a render. A successful status
means the API object is reachable, **not** that GPU/renderer/license readiness
has already been tested. [Official rendering example](https://help.solidworks.com/2026/english/api/swvisualizeapi/SolidWorks.Visualize.Interfaces~SolidWorks.Visualize.Interfaces.IVisualizeAddinManager.html),
[loading and accessing add-ins](https://help.solidworks.com/2018/english/api/sldworksapiprogguide/Overview/Accessing_Add-ins.htm).

## Typical MCP workflow

1. Create or activate the intended native CAD document and check its geometry.
2. Call `pro_visualize_status` with `{}`. If the installed add-in is unloaded,
   call it with `{"load_if_missing": true}`, or enable it in Tools > Add-ins.
   Resolve installation/license failures before scheduling the render.
3. Call `pro_visualize_render` with a unique request ID:

   ```json
   {
     "request_id": "mounting-plate-render-01",
     "width": 1280,
     "height": 720,
     "passes": 250,
     "denoiser": false,
     "image_format": "png"
   }
   ```

4. Save its `operation_id`. Poll `pro_operation`, for example:

   ```json
   {"operation_id": "<returned operation_id>", "wait_seconds": 1}
   ```

5. When the operation finishes successfully, inspect the returned image and
   artifact metadata. Evaluate composition and materials as well as file validity.

Render time depends on scene complexity, samples, resolution and hardware. A
client-side timeout does not stop the native renderer. Poll the existing
operation; do not create another request ID merely because the first call is
taking time. The same CAD writer lease remains occupied while rendering.

## Render controls

| MCP argument | Meaning | Server default / bounds |
|---|---|---|
| `request_id` | Request deduplication within this server process | Required |
| `width` | Image width in pixels | 1280; 64–4096 |
| `height` | Image height in pixels | 720; 64–4096 |
| `passes` | Visualize `FrameCount` quality target | 250; 1–5000 |
| `denoiser` | Visualize AI denoiser | `false` |
| `image_format` | Verified output format | `png` or `jpeg`; default `png` |

The adapter sets frame-count termination (`QualityLevel`). Denoising can reduce
visible sampling noise, but may change fine detail; review the image when detail
matters. The renderer's original option values are restored after completion or
failure, and restoration failures are surfaced rather than hidden.
[Denoiser API](https://help.solidworks.com/2026/english/api/swvisualizeapi/SolidWorks.Visualize.Interfaces~SolidWorks.Visualize.Interfaces.IRenderOptions~DenoiserEnabled.html),
[termination enum](https://help.solidworks.com/2025/english/api/swvisualizeapi/SolidWorks.Visualize.Interfaces~SolidWorks.Visualize.Interfaces.TerminationMode_e.html).

## Materials, scene and camera

The active SOLIDWORKS document's appearance, scene and viewing setup are the
source for rendering. v0.4 adds native controls on that exact saved document:

- `pro_visualize_appearances` inspects or applies local `.p2m` appearances to
  exact assembly component names. Optional RGB is checked after application and
  can fail if the native material resets its color.
- `pro_visualize_scene` inspects or applies a local scene/HDRI, rotation,
  backdrop and floor settings, and disables explicitly identified native lights.
  Use `dry_run:true` for current scene and light IDs. Preset insertion can replace
  IDs, so insert and inspect before a separate ID-based light operation.
- `pro_visualize_camera` inspects or changes the active model view: standard or
  custom direction, fit, zoom and perspective. It does not create a named camera
  or expose focal length, depth of field, animation or turntable controls.

These controls do not automatically save the CAD document. Failed native
readbacks are reported; earlier changes in a batch can remain. Review a small
actual render to check the material, lighting and composition transfer.

Scene preset insertion requires RealView; the tested GPU/installation could not
enable it, while direct HDRI assignment to an existing scene worked. Native
floor properties can reject or couple changes. PhotoView rendering brightness
is not a public MCP argument: a native 0.8-to-2.0 change passed readback but
did not demonstrate an exposure effect in the controlled Visualize comparison. Plain/gradient backdrop transfer also needs a
rendered check, even when the native HDRI path is preserved.

For a scene that needs further Visualize editing, export a native project:

```json
{"grouping": "part"}
```

Send that argument to `pro_visualize_export` on SOLIDWORKS 2026+. It returns a
new `.svpj` path. `appearance`, `part` and `none` choose geometry grouping; they
are not rendering presets. On SOLIDWORKS 2025, explicitly request `appearance`
to use the legacy export route. Its result labels the actual grouping
`legacy_default` because the old API has no explicit grouping parameter.
On SOLIDWORKS 2026+, pass the returned path to `pro_visualize_open_project` to
request opening through native `LaunchVisualize`. `launch_requested` confirms
API acceptance; `project_loaded:null` leaves loading unverified. PRO does not
control arbitrary scenes inside the separate Visualize application.
[Native SVPJ export API](https://help.solidworks.com/2026/english/api/swvisualizeapi/SolidWorks.Visualize.Interfaces~SolidWorks.Visualize.Interfaces.IVisualizeAddinManager2~SaveWithPartGrouping.html).

## What successful verification establishes

A render reports success only after the native `Render()` method returns true
and at least one output image fully decodes with the requested dimensions and
format. Results include file sizes and SHA-256 hashes. Existing files are not
overwritten. These checks establish a valid, newly generated output; they do not
prove that the lighting, materials or scene composition meet the design brief.

SVPJ export checks that a new non-empty file exists and hashes it. Scene fidelity
still needs a successful reopen and inspection in Visualize. An installed
Visualize executable or a reachable COM manager alone is not proof of a
successful render.

## Troubleshooting

| Result | Next action |
|---|---|
| `VISUALIZE_UNAVAILABLE` | Enable the Visualize add-in, check its installation/license, then probe again. |
| `VISUALIZE_LOAD_FAILED` | Inspect `load_code`: 3 means missing file, 4 disabled add-ins, 5 conflict, 6 registration, 7 license. |
| `VISUALIZE_VERSION_UNSUPPORTED` | Use SW2025+ for render; use SW2026+ for explicit grouping. |
| `NO_ACTIVE_DOCUMENT` | Create or activate a native part/assembly as the MCP target. |
| `RENDER_FAILED` | Inspect Visualize/SOLIDWORKS dialogs, renderer availability and license state. Do not label partial output successful. |
| `RENDER_OUTPUT_MISSING` or `RENDER_OUTPUT_INVALID` | Inspect the job directory and renderer state; output was not verified. |
| `RENDER_SETTINGS_RESTORE_FAILED` | Review the named Visualize settings before starting another job. |
| Operation still running | Poll the same operation; wait for native completion. |

No undocumented command-line render switches or private Queue/Boost protocol
are used. SOLIDWORKS help pages titled "Visualize Command Line Properties"
describe installer properties. Visualize Professional's queue is a separate
product workflow and is not automatically exposed by PRO.
[Installer properties](https://help.solidworks.com/2025/English/Installation/install_guide/r_visualize_command_line_props.htm),
[Visualize Queue](https://help.solidworks.com/2026/english/Visualize/r_queue_options_dialog_box.htm).
