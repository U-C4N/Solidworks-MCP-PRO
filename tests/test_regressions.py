"""Offline reproductions of PRO defects found in the comparative review.

Every backend is synthetic; these tests never attach to or modify SolidWorks.
"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from mcp import types
from jsonschema import Draft202012Validator, ValidationError
import pytest

from solidworks_mcp_pro.backend import NativeBackend
from solidworks_mcp_pro.server import ProApplication
from solidworks_mcp_pro.workflow import WorkflowEngine
from solidworks_mcp_pro.validation import augment_schema, normalize_args, validate_result


def object_schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required)}


NATIVE_SCHEMAS = {
    "new_part": object_schema(),
    "add_box": object_schema({field: {"type": "number"} for field in ("width_mm", "height_mm", "depth_mm")},
                             ("width_mm", "height_mm", "depth_mm")),
    "add_extruded_profile": object_schema({"points_mm": {"type": "array", "items": {}}, "depth_mm": {"type": "number"}},
                                          ("points_mm", "depth_mm")),
    "set_dimension": object_schema({"dimension_name": {"type": "string"}, "value_mm": {"type": "number"}},
                                   ("dimension_name", "value_mm")),
}


class SentinelBackend:
    instructions = "Synthetic backend, no COM."

    def __init__(self):
        self.calls = []

    async def list_tools(self):
        return [types.Tool(name=name, inputSchema=deepcopy(schema)) for name, schema in NATIVE_SCHEMAS.items()]

    async def call(self, name, arguments):
        self.calls.append((name, deepcopy(arguments)))
        if name == "add_box" and arguments["width_mm"] <= 0:
            return {"ok": False, "error": "width must be positive"}
        return {"ok": True}


@pytest.fixture
async def app(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "locks"))
    backend = SentinelBackend()
    application = ProApplication(backend, tmp_path / "workspace")
    await application.initialize()
    return application, backend


@pytest.mark.parametrize("bad_coordinate", ["NaN", "Infinity", True])
async def test_nested_geometry_rejected_before_backend(app, bad_coordinate):
    application, backend = app
    result = await application.call("add_extruded_profile", {
        "points_mm": [[0, 0], [10, 0], [bad_coordinate, 5]], "depth_mm": 5,
    })
    assert result.isError
    assert backend.calls == []


async def test_integer_outside_com_double_range_rejected_before_backend(app):
    application, backend = app
    result = await application.call("add_box", {"width_mm": 10 ** 309, "height_mm": 10, "depth_mm": 5})
    assert result.isError
    assert backend.calls == []


async def test_negative_size_in_later_step_rejects_entire_plan_before_new_part(app):
    application, backend = app
    result = await application.call("pro_run_plan", {"request_id": "negative-size", "dry_run": False, "steps": [
        {"tool": "new_part", "arguments": {}},
        {"tool": "add_box", "arguments": {"width_mm": -1, "height_mm": 10, "depth_mm": 5}},
    ]})
    assert result.isError
    assert backend.calls == []


@pytest.mark.parametrize("native_result", [
    {"ok": True, "rebuild_ok": False, "broken_features": []},
    {"ok": True, "rebuild_ok": True, "broken_features": [{"name": "Cut1", "error": 1}]},
])
async def test_plan_stops_on_reported_broken_geometry(native_result):
    calls = []

    async def invoke(name, arguments):
        calls.append(name)
        return native_result if name == "set_dimension" else {"ok": True}

    engine = WorkflowEngine(NATIVE_SCHEMAS, invoke)
    result = await engine.execute([
        {"tool": "set_dimension", "arguments": {"dimension_name": "D1@Sketch1", "value_mm": 10}},
        {"tool": "new_part", "arguments": {}},
    ], request_id="broken", dry_run=False)
    assert not result["ok"]
    assert calls == ["set_dimension"]
    assert result["steps"][0]["result"]["error_code"] == "CAD_POSTCONDITION_FAILED"


def fake_native_writer():
    calls = []

    async def call_tool(name, arguments):
        calls.append((name, deepcopy(arguments)))
        path = Path(arguments["path"])
        suffix = {"save_part": ".sldprt", "save_assembly": ".sldasm"}.get(name)
        if suffix and not str(path).lower().endswith(suffix):
            path = Path(str(path) + suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"replacement")
        return {"ok": True, "path": str(path)}

    backend = NativeBackend.__new__(NativeBackend)
    backend.upstream = SimpleNamespace(_worker=None, mcp=SimpleNamespace(call_tool=call_tool))
    return backend, calls


@pytest.mark.parametrize("tool,suffix,use_stem", [
    ("save_part", ".sldprt", True), ("save_assembly", ".sldasm", True),
    ("export", ".step", False), ("screenshot", ".png", False), ("make_drawing", ".pdf", False),
])
async def test_native_write_does_not_clobber_existing_file(tmp_path, tool, suffix, use_stem):
    backend, calls = fake_native_writer()
    target = tmp_path / ("existing" + suffix)
    target.write_bytes(b"original-user-file")
    requested = target.with_suffix("") if use_stem else target
    try:
        result = await backend.call(tool, {"path": str(requested)})
    except (ValueError, FileExistsError):
        result = {"ok": False}
    assert result["ok"] is False
    assert target.read_bytes() == b"original-user-file"
    assert not calls


async def test_explicit_overwrite_is_consumed_before_upstream_call(tmp_path):
    backend, calls = fake_native_writer()
    target = tmp_path / "existing.sldprt"
    target.write_bytes(b"original")
    result = await backend.call("save_part", {"path": str(target), "overwrite": True})
    assert result["ok"]
    assert "overwrite" not in calls[0][1]
    assert target.read_bytes() == b"replacement"


async def test_per_component_export_requires_empty_output_folder_without_overwrite(tmp_path):
    backend, calls = fake_native_writer()
    folder = tmp_path / "parts"
    folder.mkdir()
    existing = folder / "Part1.stl"
    existing.write_bytes(b"original")
    try:
        result = await backend.call("export", {"path": str(folder / "assembly.stl"), "per_component": True})
    except (ValueError, FileExistsError):
        result = {"ok": False}
    assert result["ok"] is False and not calls
    assert existing.read_bytes() == b"original"


async def test_valid_negative_coordinates_and_dimension_values_remain_supported(app):
    application, backend = app
    result = await application.call("add_extruded_profile", {
        "points_mm": [[-10, -10], [10, -10], [0, 10]], "depth_mm": 5,
    })
    assert not result.isError
    result = await application.call("set_dimension", {"dimension_name": "x1@Sketch1", "value_mm": -10})
    assert not result.isError and len(backend.calls) == 2


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), 10 ** 309])
def test_finite_validation_reaches_nested_numeric_values(bad):
    with pytest.raises(ValueError, match="finite"):
        normalize_args("unknown", {"nested": {"values": [[0, bad]]}})


def test_unknown_contract_and_ordinary_nan_text_are_preserved():
    schema = object_schema({"label": {"type": "string"}, "unknown": {"type": "array"}})
    assert augment_schema("vendor_new_tool", schema) == schema
    assert normalize_args("vendor_new_tool", {"label": "NaN is a valid title", "unknown": ["extra"]}) == {
        "label": "NaN is a valid title", "unknown": ["extra"],
    }


def test_optional_depth_null_and_signed_coordinates_remain_valid():
    schema = object_schema({"points_mm": {"type": "array"}, "depth_mm": {"anyOf": [{"type": "number"}, {"type": "null"}]}})
    validator = Draft202012Validator(augment_schema("cut_profile", schema))
    validator.validate({"points_mm": [[-10, -5], [5, -5], [0, 10]], "depth_mm": None})
    with pytest.raises(ValidationError):
        validator.validate({"points_mm": [[-10, -5], [5, -5], [0, 10]], "depth_mm": 0})


def test_plane_profile_accepts_uniform_2d_or_3d_but_not_mixed_points():
    schema = augment_schema("add_extruded_profile_on_plane", object_schema({"points_mm": {"type": "array"}}))
    validator = Draft202012Validator(schema)
    validator.validate({"points_mm": [[0, 0], [10, 0], [0, 10]]})
    validator.validate({"points_mm": [[0, 0, 0], [0, 10, 0], [0, 0, 10]]})
    with pytest.raises(ValidationError):
        validator.validate({"points_mm": [[0, 0], [0, 10, 0], [0, 0, 10]]})


@pytest.mark.parametrize("tool,args", [
    ("add_cone", {"bottom_diameter_mm": 10, "top_diameter_mm": 20}),
    ("add_counterbore_hole", {"cbore_diameter_mm": 3, "clearance_diameter_mm": 5}),
    ("add_lofted_solid", {"profiles_mm": [[], []], "heights_mm": [0, -1]}),
    ("add_lofted_solid", {"profiles_mm": [[], []], "heights_mm": [0]}),
    ("extrude_sketch", {"depth_mm": 5, "up_to": "next"}),
    ("extrude_sketch", {"sketch": "Sketch1"}),
    ("add_hole_on_face", {"face": "@NaN,0,0"}),
])
def test_state_independent_semantic_mismatches_reject_before_com(tool, args):
    with pytest.raises(ValueError):
        normalize_args(tool, args)


async def test_completed_save_plan_replays_without_rechecking_its_existing_output(tmp_path):
    backend, calls = fake_native_writer()
    schema = object_schema({"path": {"type": "string"}}, ("path",))
    engine = WorkflowEngine({"save_part": schema}, backend.call)
    steps = [{"tool": "save_part", "arguments": {"path": str(tmp_path / "saved")}}]
    first = await engine.execute(steps, request_id="save-once", dry_run=False)
    second = await engine.execute(steps, request_id="save-once", dry_run=False)
    assert first["ok"] and second["ok"] and second["replayed"]
    assert len(calls) == 1


async def test_duplicate_output_targets_reject_whole_plan_before_first_write(tmp_path):
    backend, calls = fake_native_writer()
    schema = object_schema({"path": {"type": "string"}}, ("path",))
    engine = WorkflowEngine({"save_part": schema}, backend.call)
    steps = [{"tool": "save_part", "arguments": {"path": str(tmp_path / "saved")}},
             {"tool": "save_part", "arguments": {"path": str(tmp_path / "saved.sldprt")}}]
    result = await engine.execute(steps, request_id="duplicate", dry_run=False)
    assert not result["ok"] and not calls
    assert "OUTPUT_REUSED" in result["error"]


def test_postcondition_failure_preserves_evidence_without_mutating_upstream_result():
    source = {"ok": True, "rebuild_ok": False, "broken_features": [{"name": "Cut1"}]}
    result = validate_result(source)
    assert source["ok"] is True
    assert result["ok"] is False and result["cad_reported_ok"] is True
    assert result["broken_features"] == source["broken_features"]
    assert result["changes_may_remain"]


@pytest.mark.parametrize("tool,args", [
    ("slice_mesh", {"heights_mm": []}),
    ("compare_with_mesh", {"heights_mm": [], "offset_mm": [-10, 0, 0]}),
    ("list_edges", {"min_length_mm": -1}),
    ("add_swept_pipe", {"path_mm": [[0, 0], [10, 0]], "bend_radius_mm": -1}),
    ("check_motion", {"values": [-45, 0, 45], "distances": []}),
])
def test_ignored_parameters_and_empty_read_only_queries_preserve_native_contract(tool, args):
    properties = {field: {"type": "array" if isinstance(value, list) else "number"}
                  for field, value in args.items()}
    schema = augment_schema(tool, object_schema(properties))
    Draft202012Validator(schema).validate(normalize_args(tool, args))


@pytest.mark.parametrize("tool,args", [
    ("add_cone", {"bottom_diameter_mm": 20, "top_diameter_mm": 0, "height_mm": 5}),
    ("add_rib", {"start_mm": [-10, -5], "end_mm": [10, -5], "toward_mm": [0, -10],
                 "thickness_mm": 1, "z_mm": 0}),
    ("extrude_sketch", {"depth_mm": None, "up_to": "next"}),
    ("extrude_sketch", {"depth_mm": None, "up_to": "@-5,-10,0"}),
    ("extrude_sketch", {"depth_mm": 5, "up_to": None}),
])
def test_boundary_values_retained_after_source_contract_review(tool, args):
    normalize_args(tool, args)
