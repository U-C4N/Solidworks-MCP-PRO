"""Fixed macro generation/result contracts; never execute native macros."""
from pathlib import Path

import pytest

from solidworks_mcp_pro import visualize_macro as macro


class FakeApplication:
    def __init__(self, *, returned=(True, 0), marker="success", exception=None, export_path=None):
        self.returned = returned
        self.marker = marker
        self.exception = exception
        self.export_path = export_path
        self.calls = []

    def RunMacro2(self, path, module, procedure, options):
        self.calls.append((path, module, procedure, options))
        source = Path(path).read_text(encoding="ascii")
        assert 'Application.SldWorks' in source
        assert 'SolidWorks.Visualize.Implementation.VisualizeAddin' in source
        assert module == "" and procedure == "main" and options == 1
        nonce = Path(path).stem.removeprefix("pro_visualize_")
        if self.marker != "missing":
            start = "START|" + ("different" if self.marker == "wrong_nonce" else nonce)
            accepted = "EXPORT_RETURNED" if self.export_path else "RENDER_TRUE"
            lines = [start, "STAGE|render", accepted, "DONE|" + nonce]
            if self.marker == "false":
                lines[2] = "RENDER_FALSE"
            elif self.marker == "incomplete":
                lines.pop()
            elif self.marker == "error":
                lines.insert(2, "ERROR|-2147467259|native failure")
            Path(path).with_suffix(".result").write_text("\n".join(lines), encoding="ascii")
        if self.exception:
            raise self.exception
        if self.export_path:
            self.export_path.write_bytes(b"verified existence only")
        return self.returned


def test_fixed_render_runs_once_and_retains_marker_and_source(tmp_path):
    app = FakeApplication()
    result = macro.render_on_ui(app, tmp_path)
    assert result["ok"] and result["accepted"]
    assert result["execution"] == "solidworks_macro"
    assert len(app.calls) == 1
    source = Path(result["macro_path"]).read_text(encoding="ascii")
    assert "Set manager = addin.GetAddinManager()" in source
    assert "accepted = manager.Render()" in source
    assert "On Error GoTo Failed" in source
    assert "Err.Number" in source and "Err.Description" in source
    assert Path(result["marker_path"]).is_file()


@pytest.mark.parametrize("returned", [False, True, (False, 0), (True, 1), (True, False), (1, 0), (True,)])
def test_runmacro_failure_never_passes_even_with_success_marker(tmp_path, returned):
    with pytest.raises(macro.VisualizeMacroError, match="VISUALIZE_MACRO_RUN_FAILED"):
        macro.render_on_ui(FakeApplication(returned=returned), tmp_path)


@pytest.mark.parametrize("marker", ["missing", "wrong_nonce", "false", "incomplete", "error"])
def test_macro_return_does_not_override_missing_or_failed_completion(tmp_path, marker):
    with pytest.raises(macro.VisualizeMacroError, match="VISUALIZE_MACRO_INCOMPLETE"):
        macro.render_on_ui(FakeApplication(marker=marker), tmp_path)


def test_exception_retains_stage_error_and_diagnostic_paths(tmp_path):
    app = FakeApplication(marker="error", exception=RuntimeError("COM transport failed"))
    with pytest.raises(macro.VisualizeMacroError) as error:
        macro.render_on_ui(app, tmp_path)
    text = str(error.value)
    assert "COM transport failed" in text and "native failure" in text
    assert "STAGE|render" in text
    assert list(tmp_path.glob("*.swb")) and list(tmp_path.glob("*.result"))


def test_macro_result_is_bounded(tmp_path):
    class LargeResult(FakeApplication):
        def RunMacro2(self, path, *args):
            Path(path).with_suffix(".result").write_bytes(b"x" * 65537)
            return True, 0

    with pytest.raises(macro.VisualizeMacroError, match="64 KiB"):
        macro.render_on_ui(LargeResult(), tmp_path)


def test_vba_string_keeps_quotes_newlines_and_unicode_inside_expression():
    expression = macro._vba_string('C:\\a"b\nş😀.svpj')
    assert expression == '"C:\\a""b" & ChrW(10) & "" & ChrW(351) & "" & ChrW(-10179) & "" & ChrW(-8704) & ".svpj"'
    assert expression.isascii() and "\n" not in expression


def test_unknown_action_never_creates_macro(tmp_path):
    app = FakeApplication()
    with pytest.raises(macro.VisualizeMacroError, match="INVALID_MACRO_ACTION"):
        macro.invoke(app, "user supplied code", tmp_path)
    assert not app.calls and not list(tmp_path.iterdir())


def test_missing_scratch_directory_never_creates_or_runs(tmp_path):
    app = FakeApplication()
    with pytest.raises(macro.VisualizeMacroError, match="INVALID_MACRO_DIRECTORY"):
        macro.render_on_ui(app, tmp_path / "missing")
    assert not app.calls


def test_fixed_export_requires_nonempty_output(tmp_path):
    target = tmp_path / "project.svpj"
    result = macro.export_on_ui(FakeApplication(export_path=target), target, 1, tmp_path)
    source = Path(result["macro_path"]).read_text(encoding="ascii")
    assert "Set manager = addin.GetAddinManager2()" in source
    assert 'Call manager.SaveWithPartGrouping(' in source
    assert result["accepted"] and target.stat().st_size > 0


def test_export_marker_alone_does_not_prove_output(tmp_path):
    class MissingExport(FakeApplication):
        def RunMacro2(self, path, *args):
            nonce = Path(path).stem.removeprefix("pro_visualize_")
            Path(path).with_suffix(".result").write_text(
                f"START|{nonce}\nEXPORT_RETURNED\nDONE|{nonce}", encoding="ascii")
            return True, 0

    with pytest.raises(macro.VisualizeMacroError, match="VISUALIZE_MACRO_OUTPUT_MISSING"):
        macro.export_on_ui(MissingExport(), tmp_path / "project.svpj", 0, tmp_path)


@pytest.mark.parametrize("grouping", [True, -1, 3, "1"])
def test_export_rejects_unvalidated_grouping(tmp_path, grouping):
    app = FakeApplication()
    with pytest.raises(macro.VisualizeMacroError, match="INVALID_MACRO_EXPORT"):
        macro.export_on_ui(app, tmp_path / "project.svpj", grouping, tmp_path)
    assert not app.calls


def test_export_refuses_existing_project_without_macro_execution(tmp_path):
    target = tmp_path / "project.svpj"
    target.write_bytes(b"keep")
    app = FakeApplication()
    with pytest.raises(macro.VisualizeMacroError, match="INVALID_MACRO_EXPORT"):
        macro.export_on_ui(app, target, 0, tmp_path)
    assert target.read_bytes() == b"keep" and not app.calls
