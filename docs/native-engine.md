# Independent native engine

Version 0.7.0 implements all 88 CAD tool handlers in this repository. The former
external CAD MCP dependency and adapted mutation guard have been replaced.
The protocol layer still uses the official Python MCP SDK; CAD operations use the
user's installed SOLIDWORKS SDK and licensed application. This is an automation
engine, not a replacement for the SOLIDWORKS geometry kernel.

## Execution and evidence

Native COM references stay on one bounded STA worker. Schema validation and the
operation lease precede dispatch. Existing documents require `allow_edit:true`
when opening or activating before modeling; newly created documents are editable.
A user changing the active document cannot silently redirect a mutation.

Mutations record native undo availability, snapshot the observed feature tree,
configuration, feature errors and volume, then check the resulting feature errors.
A failure attempts native undo when the original target remains active. The
receipt reports the exact verification scope; missing or ineffective undo remains
unverified. It does not claim a complete internal CAD state snapshot. Selection
marks, command mode and UI state are restored with readback checks.

Document closing protects other hidden documents from native `CloseDoc` side
effects and restores their visibility. Output collision checks remain active.
Topology references are bound to document, configuration and model revision and
must be refreshed after modeling. Tools do not expose arbitrary code execution.

## Tool catalogue

### Documents (14)

`activate_document`, `close_part`, `export`, `get_status`, `list_documents`, `make_drawing`, `new_assembly`, `new_part`, `open_assembly`, `open_part`, `rebuild`, `save_assembly`, `save_part`, `screenshot`.

### Geometry (30)

`add_boss_on_face`, `add_box`, `add_cone`, `add_counterbore_hole`, `add_cylinder`, `add_disc`, `add_extruded_profile`, `add_extruded_profile_on_face`, `add_extruded_profile_on_plane`, `add_extruded_slot`, `add_extruded_spline`, `add_hole`, `add_hole_on_face`, `add_hole_wizard`, `add_lofted_solid`, `add_plane`, `add_revolved_profile`, `add_rib`, `add_sketch`, `add_swept_pipe`, `add_swept_profile`, `add_text_on_face`, `cut_offset_pocket`, `cut_profile`, `cut_profile_on_face`, `cut_profile_through_plane`, `cut_sketch`, `cut_slot`, `extrude_sketch`, `loft_sketches`.

### Features (24)

`add_chamfer`, `add_circular_pattern`, `add_fillet`, `add_full_round`, `add_linear_pattern`, `add_mirror`, `add_shell`, `add_thread`, `combine_bodies`, `delete_equation`, `delete_feature`, `list_bodies`, `list_dimensions`, `list_equations`, `list_features`, `read_sketch`, `reorder_feature`, `set_appearance`, `set_dimension`, `set_equation`, `set_equations`, `set_material`, `split_body`, `suppress_feature`.

### Mesh (3)

`compare_with_mesh`, `slice_mesh`, `swept_region`.

### Assembly (15)

`add_mate`, `check_interference`, `check_motion`, `check_printability`, `delete_component`, `delete_mate`, `get_assembly_bounding_box`, `get_bounding_box`, `get_mass_properties`, `insert_component`, `list_components`, `measure_distance`, `replace_component`, `set_component_transform`, `suppress_mate`.

### Topology (2)

`list_edges`, `list_faces`.

## Tested scope and remaining limits

Unit tests, protocol discovery and real native acceptance are separate checks.
The live runner has one owned fixture per tool, analytic geometry expectations
where applicable, explicit numeric tolerances, source hashes and cleanup evidence.
A failed prerequisite is reported as blocked, never counted as a native pass.
The engine is exercised with the external CAD MCP package uninstalled.

```powershell
.\.venv\Scripts\python.exe scripts\validate_native_core_live.py --live --group all --output artifacts\core-acceptance-new
```

This creates new scratch documents and requires an initially empty running CAD
session. It does not adopt or close existing user documents.

A successful fixture establishes the documented inputs on the tested installation.
It does not establish every optional parameter or every SOLIDWORKS version.
Printability reports remain screening evidence with unknown checks; sampled
motion and swept-region envelopes do not prove continuous collision freedom.
Mesh comparison uses tessellated native geometry with explicit tolerances.
Rendering additionally depends on the installed Visualize license and API.

Historical performance reports measure their named releases. No new engine speed
advantage is claimed without a new controlled benchmark.
