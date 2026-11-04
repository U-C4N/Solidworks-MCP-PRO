"""Fixed internal Visualize calls through SOLIDWORKS' supported macro engine.

No caller-supplied code or public macro tool is accepted. The owning CAD worker
and lease must remain held until RunMacro2 returns. Files are retained as
diagnostic evidence; request cancellation must not remove a running macro.
"""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4


class VisualizeMacroError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _vba_string(value: str) -> str:
    """ASCII VBA expression, including escaped quotes and UTF-16 code units."""
    chunks = ['"']
    units = value.encode("utf-16-le")
    for index in range(0, len(units), 2):
        unit = int.from_bytes(units[index:index + 2], "little")
        if 32 <= unit <= 126:
            chunks.append('""' if unit == 34 else chr(unit))
        else:
            chunks.append(f'" & ChrW({unit if unit < 32768 else unit - 65536}) & "')
    chunks.append('"')
    return "".join(chunks)


def _source(marker: Path, nonce: str, action: str, project: Path | None, grouping: int) -> str:
    if action == "render":
        statements = '''    Set manager = addin.GetAddinManager()
    Call WriteResult("STAGE|render")
    accepted = manager.Render()
    If accepted Then
        Call WriteResult("RENDER_TRUE")
    Else
        Call WriteResult("RENDER_FALSE")
    End If'''
    else:
        statements = f'''    Set manager = addin.GetAddinManager2()
    Call WriteResult("STAGE|export")
    Call manager.SaveWithPartGrouping({_vba_string(str(project))}, {grouping})
    Call WriteResult("EXPORT_RETURNED")'''
    return f'''Dim resultPath As String
Sub WriteResult(ByVal value As String)
    Dim fn As Integer
    fn = FreeFile
    Open resultPath For Append As #fn
    Print #fn, value
    Close #fn
End Sub
Sub main()
    Dim swApp As Object
    Dim addin As Object
    Dim manager As Object
    Dim accepted As Boolean
    Dim failureNumber As Long
    Dim failureText As String
    On Error GoTo Failed
    resultPath = {_vba_string(str(marker))}
    Call WriteResult("START|{nonce}")
    Call WriteResult("STAGE|application")
    Set swApp = Application.SldWorks
    Call WriteResult("STAGE|addin")
    Set addin = swApp.GetAddInObject("SolidWorks.Visualize.Implementation.VisualizeAddin")
    Call WriteResult("STAGE|manager")
{statements}
    Call WriteResult("DONE|{nonce}")
    Exit Sub
Failed:
    failureNumber = Err.Number
    failureText = Err.Description
    On Error Resume Next
    Call WriteResult("ERROR|" & CStr(failureNumber) & "|" & Replace(Replace(failureText, vbCr, " "), vbLf, " "))
    Call WriteResult("FAILED|{nonce}")
End Sub
'''.replace("\n", "\r\n")


def invoke(sw_app, action: str, scratch_dir: Path, *, project_path: Path | None = None, grouping: int = 0) -> dict:
    """Invoke only the fixed render/export template and verify its completion."""
    if action not in {"render", "export"}:
        raise VisualizeMacroError("INVALID_MACRO_ACTION", "Only fixed render/export actions are available.")
    folder = Path(scratch_dir)
    if not folder.is_absolute() or not folder.is_dir() or folder.is_symlink():
        raise VisualizeMacroError("INVALID_MACRO_DIRECTORY", "An existing absolute non-linked job directory is required.")
    folder = folder.resolve(strict=True)
    project = None
    if action == "export":
        if type(grouping) is not int or grouping not in (0, 1, 2) or project_path is None:
            raise VisualizeMacroError("INVALID_MACRO_EXPORT", "An absolute new .svpj path and grouping 0/1/2 are required.")
        project = Path(project_path)
        if not project.is_absolute() or project.suffix.lower() != ".svpj" or project.exists() or project.is_symlink():
            raise VisualizeMacroError("INVALID_MACRO_EXPORT", "Export refuses existing targets and requires an absolute .svpj path.")
    nonce = uuid4().hex
    macro = folder / ("pro_visualize_" + nonce + ".swb")
    marker = folder / ("pro_visualize_" + nonce + ".result")
    # Exclusive creation, no shell, no arbitrary module/procedure or source.
    with macro.open("x", encoding="ascii", newline="") as stream:
        stream.write(_source(marker, nonce, action, project, grouping))
    run_error = None
    returned = None
    try:
        returned = sw_app.RunMacro2(str(macro), "", "main", 1)
    except Exception as exc:
        run_error = str(exc)
    lines = []
    if marker.is_file():
        if marker.stat().st_size > 65536:
            raise VisualizeMacroError("MACRO_RESULT_INVALID", f"Macro result exceeds 64 KiB: {marker}")
        lines = marker.read_bytes().decode("mbcs" if os.name == "nt" else "utf-8", errors="replace").splitlines()
    details = f"RunMacro2={returned!r}; exception={run_error!r}; marker={lines[-8:]!r}; macro={macro}"
    if (run_error is not None or not isinstance(returned, (tuple, list)) or len(returned) != 2
            or returned[0] is not True or type(returned[1]) is not int or returned[1] != 0):
        raise VisualizeMacroError("VISUALIZE_MACRO_RUN_FAILED", details)
    expected = "RENDER_TRUE" if action == "render" else "EXPORT_RETURNED"
    if (not lines or lines[0] != "START|" + nonce or lines[-1] != "DONE|" + nonce
            or expected not in lines or any(line.startswith(("ERROR|", "FAILED|")) for line in lines)):
        raise VisualizeMacroError("VISUALIZE_MACRO_INCOMPLETE", details)
    if action == "export" and (not project.is_file() or project.stat().st_size == 0):
        raise VisualizeMacroError("VISUALIZE_MACRO_OUTPUT_MISSING", f"The macro returned without a nonempty project: {project}")
    return {"ok": True, "accepted": True, "execution": "solidworks_macro", "action": action,
            "macro_path": str(macro), "marker_path": str(marker), "run_macro_error": returned[1],
            "verification": "RunMacro2 succeeded and the unique fixed-macro completion marker matched; output verification is separate."}


def render_on_ui(sw_app, output_folder: Path) -> dict:
    return invoke(sw_app, "render", output_folder)


def export_on_ui(sw_app, project_path: Path, grouping: int, scratch_dir: Path) -> dict:
    return invoke(sw_app, "export", scratch_dir, project_path=project_path, grouping=grouping)
