# Independent native SOLIDWORKS engine

All 88 compatibility tool handlers are implemented inside this package. No
external CAD MCP engine is imported. Native COM bindings are generated from
the installed SOLIDWORKS SDK. Public inputs/measurements use mm, degrees and kg;
native calculations use metres/radians/SI and are converted explicitly.

Start SOLIDWORKS and call get_status. It attaches to an already running instance,
never launches one on a status query. new_part/new_assembly use the installed
default templates. list_documents inventories open documents without activation.

The default box profile is Front/XY and its depth is along +Z. Native world up
is +Y; Top is XZ and Right is YZ. Plane aliases use reference-plane order rather
than translated UI names. Geometry on a face uses that face's sketch transform.

Freshly created documents are editable. Opening or activating an existing native
document does not automatically authorize modeling: explicitly set allow_edit
true for that target when the user has requested edits. The kernel refuses a
different active document, read-only/view-only mode and unfinished user edits.
No call silently activates another document to satisfy a stale session target.

Validate schemas and input relationships before native mutation. Native feature
results include native names, types and measurements where available. Reference
planes/sketches can validly exist before a solid, so their receipts can contain
an explicit measurement warning instead of a fabricated positive volume.

Mutations use the owning STA worker and a native undo record. UI, selection and
command flags are restored before ownership is released. On failure the engine
attempts native undo, compares its observed feature/configuration/volume
signature and reports the scope. This is not a guarantee that arbitrary physical
design intent or all application side effects were reversed. Unverified state
must be inspected, not retried blindly.

Face and edge IDs are bound to document/configuration/update state and expire
after edits. Re-list entities after changes. Explicit point selectors have a
bounded tolerance; nearest entities outside it are refused. Selection marks are
set explicitly for the required native feature operation.

save_part/save_assembly write native documents. export supports native STEP,
IGES, Parasolid and mesh writers, reports actual files/hashes, and restores
global export preferences. Public server calls refuse overwrite by default.
screenshot captures the real viewport and restores view/preferences; it is not
a Visualize render. make_drawing creates first-angle views plus isometric output
from a saved part, writes PDF or SLDDrw, then restores the source document.

Read-only geometry queries avoid rebuilding. Native density/overrides determine
mass; no material strength is inferred. Motion and swept-region checks sample
explicit dimension values and restore the dimension; they do not establish
continuous collision-free motion or dynamic mechanical correctness. Mesh
comparison/slicing states its coordinate frame and sampling limitations.

Raw code/macros are not exposed as native tool inputs. API failure and missing
installed templates/assets remain explicit failures, never placeholder success.
