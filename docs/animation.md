# Native CAD animation and Visualize project frames

PRO can prepare an assembly state, create a geometric sheet bend, export the
current CAD state, and render a native Visualize project with a fixed camera.
Each changing geometry state is a real SOLIDWORKS solid. The current video
orchestrator is a task-local script; there is no public one-call video tool.

## Tools and units

| Tool | Purpose and limits |
| --- | --- |
| `pro_assembly_configurations` / `pro_assembly_configure` | Inspect configurations or select an exact assembly/component configuration. Uses exact saved document and component names. |
| `pro_assembly_pose` | Set absolute component translations in millimetres and Euler angles in degrees. This does not solve mates; apply poses after dimension changes/rebuilds. |
| `pro_assembly_visibility` | Set exact top-level component visibility, with native readback. `components: []` is permitted only with `dry_run: true` for inventory. No rebuild or save. |
| `pro_sheet_bend` | Create/update a marked `MCP_Bend` configuration and body in an exact saved, writable part copy. Native volume and bounds are checked. No automatic save. |
| `pro_visualize_named_camera` | Inspect, apply, activate, deactivate or delete an owned `MCP_` camera. Position/target use millimetres; roll uses degrees. Lens fields are readback-only. |
| `pro_visualize_export` | Export the current native model state to a new Visualize project. |
| `pro_visualize_render_project` | Render an owned SVPJ copy using the installed Visualize Offline Renderer. Optionally copy its active camera from a reference SVPJ or set explicit position, target and vertical field of view. |

The model-changing tools require the exact active saved document. Work on an
isolated assembly and isolated referenced parts. A renamed assembly that still
references the original parts does not isolate spring or sheet edits. The tools
do not infer that an arbitrary writable file is a disposable animation copy.

`pro_visualize_render_project` requires a `request_id`. It returns a running
operation when rendering continues asynchronously; poll `pro_operation` with the
returned `operation_id`. Reuse of a running request is not a second render. The
operation retains PRO's serialized CAD lease until native completion. Separate
CAD automation clients are outside that lock.

## Geometric sheet bending

The default sheet has developed profile length 90 mm, extrusion depth 80 mm,
thickness 1.5 mm, a 36 mm central flat and 2 mm inner bend radius. It lies in the
YZ plane and is extruded symmetrically along X. `bottom_offset_mm` translates the
profile vertically. `angle_deg` accepts exactly zero or 0.01 through 90 degrees.

The two bends use circular inner/outer arcs and a neutral radius halfway through
the thickness. Straight flange length decreases by the neutral-axis arc length,
preserving developed length and the 10,800 mm³ volume. Zero degrees produces an
exact rectangular profile. The tool builds and validates a temporary native
solid before changing the owned feature. It creates the feature with
[`CreateFeatureFromBody3`](https://help.solidworks.com/2026/english/api/sldworksapi/SOLIDWORKS.Interop.sldworks~SOLIDWORKS.Interop.sldworks.IPartDoc~CreateFeatureFromBody3.html)
and replaces its body with
[`SetBody2`](https://help.solidworks.com/2024/English/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.IFeature~SetBody2.html).

Callers explicitly list the original solid features to suppress in the owned
configuration. Existing unmarked configurations/features are not adopted or
overwritten. Original configurations remain available; the new bend body is
suppressed there. Dependent features on the owned body prevent replacement.
On failure, restoration is attempted and reported; a partially changed model is
not described as successfully rolled back unless restoration was verified.

This is constant-thickness geometric animation. The neutral-axis factor of 0.5
is a modeling assumption, not a material model. It does not calculate forming
forces, plastic strain, thinning, springback, damage or manufacturing feasibility.
Tool clearance and contact placement require a separate motion model and checks.

## Preparing a frame sequence

1. Isolate the assembly and references, inspect configurations, then prepare
   explicit setup and restoration checkpoints.
2. Create the owned sheet configuration; select the sheet and spring component
   configurations once. Suppress only the placement mate needed for the planned
   absolute sheet motion.
3. For each frame, set sheet bend and offset, spring height and lever dimension;
   apply the final sheet pose after all rebuilds. Use absolute changing values.
4. Set the visibility context, export that state to a new SVPJ, then render with
   the matching camera reference. Validate the completed PNG before using it.
5. Restore the declared final checkpoint and mate/visibility state. Save only
   through a separate explicit action.

Named SOLIDWORKS cameras alone did not guarantee stable Visualize framing on the
tested installation: the add-in could fit visible parts during export. Project
rendering instead verifies the active camera in the resulting SVPJ. Reference
camera data is copied onto current geometry, not used as a replacement scene.
An isolated-part result shot is a visibility/camera cut, not evidence that the
mechanism physically unloaded that part.

The installed SOLIDWORKS Visualize 2026 Offline Renderer route is based on its
verified executable metadata and native pilot output; it is not a documented
COM animation-export contract. Its `-frames` argument denotes rendering passes,
not a movie frame count. PRO creates a new project copy, validates package/XML
contents, preserves source projects, checks renderer availability, and verifies
the output format, dimensions and hash. Public render sizes are 128–4096 pixels
per dimension and pass counts are 1–5000. Ground reflections require shadows.

## Kollu Pres V2 validation snapshot

On the tested SOLIDWORKS 2026 SP4.1 installation, native 0°, 45° and 90° sheet
updates retained 10,800 mm³ volume within floating-point tolerance. Eleven
sampled assembly states reported zero native interferences. These samples do
not prove clearance at every intermediate angle. A separate native project
pilot rendered a verified 1280×720 PNG with explicit camera coordinates and
ground reflections disabled.

The task-local plan contains 92 motion states plus one isolated U-result shot:
feed along Z, approach, bending and press retraction. Retraction retains the
90° U shape. Holds produce 336 output frames at 24 fps (14 seconds); holds do not
invent intermediate geometry. The requested black spray-paint appearance is
assigned at assembly level to the sheet and does not change its engineering
material. These are plan properties; successful final video rendering is a
separate acceptance step.

Task-local builders, plans and native evidence live under `.local/` and are not
included in the source distribution. They contain this workstation's CAD and
camera-reference paths. Public MCP bridges and their mocked tests are included;
mocked tests do not replace native CAD/render acceptance.
