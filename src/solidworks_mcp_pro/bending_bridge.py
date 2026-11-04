"""Analytic constant-thickness U bending on an explicitly owned part copy.

The profile lies in YZ, extruded symmetrically along X. Circular bends use
inner R and outer R+t; a mid-thickness neutral axis conserves developed length
and volume. This is geometric animation, not a material/forming simulation.
All native calls run on the existing CAD worker under its exclusive lease.
"""
from __future__ import annotations

import math
import os
import re
import stat
from pathlib import Path
from typing import Any

OWNER = "Solidworks-MCP-PRO:sheet-bend:v1"


class BendingError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _typed(obj: Any, name: str) -> Any:
    if obj is None or not hasattr(obj, "_oleobj_"):
        return obj
    from .native import binding
    return binding.wrap(obj, getattr(binding.module(), name))


def _number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise BendingError("INVALID_ARGUMENT", f"{name} must be finite and within {low}..{high}.")
    return float(value)


def profile(angle_deg: float, *, bottom_offset_mm: float = 0, length_mm: float = 90,
            depth_mm: float = 80, thickness_mm: float = 1.5, flat_mm: float = 36,
            inner_radius_mm: float = 2) -> dict:
    """Return exact line/arc data in (Z,Y) millimetres; no CAD access."""
    angle = _number(angle_deg, "angle_deg", 0, 90)
    bottom = _number(bottom_offset_mm, "bottom_offset_mm", -1000, 1000)
    length = _number(length_mm, "length_mm", 0.1, 10000)
    depth = _number(depth_mm, "depth_mm", 0.1, 10000)
    thickness = _number(thickness_mm, "thickness_mm", 0.01, 1000)
    flat = _number(flat_mm, "flat_mm", 0.1, 10000)
    radius = _number(inner_radius_mm, "inner_radius_mm", 0.01, 1000)
    neutral = radius + thickness / 2
    # Validate full 90-degree travel, not just the requested frame.
    if flat >= length or (length - flat) / 2 - neutral * math.pi / 2 <= 0.01:
        raise BendingError("INVALID_GEOMETRY", "Developed length must leave positive straight flanges through 90 degrees.")
    theta = math.radians(angle)
    flange = (length - flat) / 2 - neutral * theta
    segments = []

    def line(a, b):
        segments.append({"kind": "line", "start": list(a), "end": list(b)})

    def arc(a, b, center, direction):
        segments.append({"kind": "arc", "start": list(a), "end": list(b),
                         "center": list(center), "direction": direction})

    if angle == 0:
        points = [(-length / 2, bottom), (length / 2, bottom),
                  (length / 2, bottom + thickness), (-length / 2, bottom + thickness)]
        for index, point in enumerate(points):
            line(point, points[(index + 1) % 4])
    else:
        if angle < 0.01:
            raise BendingError("INVALID_ARGUMENT", "Use exactly 0 or angle_deg >=0.01 to avoid degenerate native arcs.")
        half, outer = flat / 2, radius + thickness
        s, c = math.sin(theta), math.cos(theta)
        cy = bottom + outer
        ro = (half + outer * s, cy - outer * c)
        ri = (half + radius * s, cy - radius * c)
        rot = (ro[0] + flange * c, ro[1] + flange * s)
        rit = (ri[0] + flange * c, ri[1] + flange * s)
        def mirror(point):
            return (-point[0], point[1])
        line((-half, bottom), (half, bottom))
        arc((half, bottom), ro, (half, cy), 1)
        line(ro, rot)
        line(rot, rit)
        line(rit, ri)
        arc(ri, (half, bottom + thickness), (half, cy), -1)
        line((half, bottom + thickness), (-half, bottom + thickness))
        arc((-half, bottom + thickness), mirror(ri), (-half, cy), -1)
        line(mirror(ri), mirror(rit))
        line(mirror(rit), mirror(rot))
        line(mirror(rot), mirror(ro))
        arc(mirror(ro), (-half, bottom), (-half, cy), 1)
    return {"angle_deg": angle, "bottom_offset_mm": bottom, "length_mm": length,
            "depth_mm": depth, "thickness_mm": thickness, "flat_mm": flat,
            "inner_radius_mm": radius, "neutral_radius_mm": neutral,
            "flange_length_mm": flange, "expected_volume_mm3": length * thickness * depth,
            "profile_plane": "YZ", "extrusion_x_mm": [-depth / 2, depth / 2],
            "neutral_axis_factor": 0.5, "segments": segments}


def _array(values, kind="double"):
    import pythoncom
    from win32com.client import VARIANT
    types = {"double": pythoncom.VT_R8, "object": pythoncom.VT_DISPATCH, "string": pythoncom.VT_BSTR}
    return VARIANT(pythoncom.VT_ARRAY | types[kind], list(values))


def _make_body(sw_app: Any, data: dict) -> Any:
    """Official modeler recipe: trimmed analytic curves → sheet → extrusion."""
    modeler = _typed(sw_app.GetModeler(), "IModeler")
    math_util = _typed(sw_app.GetMathUtility(), "IMathUtility")
    x = -data["depth_mm"] / 2000

    def xyz(point):
        return (x, point[1] / 1000, point[0] / 1000)

    curves = []
    for segment in data["segments"]:
        start, end = xyz(segment["start"]), xyz(segment["end"])
        if segment["kind"] == "line":
            delta = [b - a for a, b in zip(start, end)]
            norm = math.sqrt(sum(value * value for value in delta))
            raw = modeler.CreateLine(_array(start), _array([value / norm for value in delta]))
        else:
            center = xyz(segment["center"])
            radius = math.dist(start, center)
            # (u,v)=(Z,Y), therefore positive planar rotation has normal -X.
            axis = (-segment["direction"], 0.0, 0.0)
            raw = modeler.CreateArc(_array(center), _array(axis), radius, _array(start), _array(end))
        if raw is None:
            raise BendingError("GEOMETRY_FAILED", f"Modeler could not create {segment['kind']}.")
        curve = _typed(raw, "ICurve").CreateTrimmedCurve2(*start, *end)
        if curve is None:
            raise BendingError("GEOMETRY_FAILED", "Modeler could not trim a profile curve.")
        curves.append(curve)
    surface = _typed(modeler.CreatePlanarSurface2(_array((x, 0, 0)), _array((-1, 0, 0)),
                                                 _array((0, 0, 1))), "ISurface")
    if surface is None:
        raise BendingError("GEOMETRY_FAILED", "Modeler returned no planar surface.")
    sheet = _typed(surface.CreateTrimmedSheet4(_array(curves, "object"), True), "IBody2")
    if sheet is None:
        raise BendingError("GEOMETRY_FAILED", "The analytic profile did not form a closed sheet.")
    vector = math_util.CreateVector(_array((1, 0, 0)))
    body = _typed(modeler.CreateExtrudedBody(sheet, vector, data["depth_mm"] / 1000), "IBody2")
    if body is None:
        raise BendingError("GEOMETRY_FAILED", "Modeler could not extrude the sheet profile.")
    return body


def _body_evidence(body: Any, expected: float | None = None) -> dict:
    body = _typed(body, "IBody2")
    if body.GetType() != 0:  # swSolidBody
        raise BendingError("SOLID_REQUIRED", "Native geometry is not a solid body.")
    values = body.GetMassProperties(1000.0)
    if not isinstance(values, (list, tuple)) or len(values) < 6:
        raise BendingError("VOLUME_UNAVAILABLE", "Native body mass properties are unavailable.")
    volume = float(values[3]) * 1e9
    if not math.isfinite(volume) or volume <= 0:
        raise BendingError("VOLUME_INVALID", "Native body volume is not positive and finite.")
    if expected is not None and not math.isclose(volume, expected, rel_tol=1e-6, abs_tol=0.001):
        raise BendingError("VOLUME_MISMATCH", f"Expected {expected:.9f} mm3, measured {volume:.9f} mm3.")
    bounds = body.GetBodyBox()
    if not isinstance(bounds, (tuple, list)) or len(bounds) != 6 or not all(math.isfinite(v) for v in bounds):
        raise BendingError("BOUNDS_UNAVAILABLE", "Native body bounds are unavailable.")
    return {"volume_mm3": volume, "bounding_box_mm": [float(value) * 1000 for value in bounds]}


def _same_file(left, right) -> bool:
    try:
        return os.path.samefile(left, right)
    except (OSError, ValueError):
        return False


def _active(sw_app, document_path):
    if not isinstance(document_path, (str, Path)) or not str(document_path) or "\x00" in str(document_path):
        raise BendingError("INVALID_ARGUMENT", "document_path must be an absolute local .sldprt file.")
    path = Path(document_path)
    if (not path.is_absolute() or str(path).startswith(("\\\\", "//")) or path.is_symlink()
            or path.suffix.lower() != ".sldprt" or not path.is_file() or not path.stat().st_size):
        raise BendingError("INVALID_ARGUMENT", "document_path must name an existing absolute local nonempty .sldprt.")
    model = _typed(sw_app.ActiveDoc, "IModelDoc2")
    if model is None or not _same_file(model.GetPathName(), path):
        raise BendingError("DOCUMENT_MISMATCH", "The exact saved part must be active.")
    if model.GetType() != 1:
        raise BendingError("PART_REQUIRED", "Sheet bending requires a part document.")
    if model.IsOpenedReadOnly() or model.IsOpenedViewOnly() or not path.stat().st_mode & stat.S_IWRITE:
        raise BendingError("DOCUMENT_READ_ONLY", "The owned part copy must be writable and editable.")
    return model, path


def _features(model) -> dict:
    result = {}
    item = _typed(model.FirstFeature(), "IFeature")
    while item is not None:
        name = str(item.Name)
        if name in result:
            raise BendingError("FEATURE_NOT_UNIQUE", f"Duplicate native feature name {name!r}.")
        result[name] = item
        item = _typed(item.GetNextFeature(), "IFeature")
    return result


def _suppressed(feature, config=None) -> bool:
    values = feature.IsSuppressed2(1 if config is None else 3, None if config is None else _array([config], "string"))
    if not isinstance(values, (tuple, list)) or len(values) != 1 or type(values[0]) is not bool:
        raise BendingError("STATE_UNAVAILABLE", "Native suppression readback is unavailable.")
    return values[0]


def _suppress(feature, value: bool, config=None):
    if not feature.SetSuppression2(0 if value else 1, 1 if config is None else 3,
                                   None if config is None else _array([config], "string")):
        raise BendingError("SUPPRESSION_FAILED", f"Could not set suppression for {feature.Name!r}.")
    if _suppressed(feature, config) is not value:
        raise BendingError("SUPPRESSION_FAILED", "Native suppression readback differs.")


def _config(model):
    return str(_typed(model.GetActiveConfiguration(), "IConfiguration").Name)


def _body(part):
    bodies = part.GetBodies2(0, False) or ()
    if len(bodies) != 1:
        raise BendingError("BODY_COUNT_MISMATCH", f"Expected one active solid; found {len(bodies)}.")
    return _typed(bodies[0], "IBody2")


def apply(sw_app: Any, document_path: str | Path, *, angle_deg: float,
          suppress_features: list[str], bottom_offset_mm: float = 0,
          configuration: str = "MCP_Bend", feature_name: str = "MCP_BendBody",
          length_mm: float = 90, depth_mm: float = 80, thickness_mm: float = 1.5,
          flat_mm: float = 36, inner_radius_mm: float = 2) -> dict:
    """Create/update owned bending geometry; no automatic save or document open.

    Success leaves the explicitly requested owned configuration active. Failure
    attempts prior body/suppression restoration and the original configuration;
    dirty flags are not claimed to be restored. Other configurations suppress
    this imported feature. Existing unmarked configurations/features are refused.
    """
    data = profile(angle_deg, bottom_offset_mm=bottom_offset_mm, length_mm=length_mm,
                   depth_mm=depth_mm, thickness_mm=thickness_mm, flat_mm=flat_mm, inner_radius_mm=inner_radius_mm)
    for value, label in ((configuration, "configuration"), (feature_name, "feature_name")):
        if not isinstance(value, str) or not re.fullmatch(r"MCP_Bend[A-Za-z0-9_\-]{0,80}", value):
            raise BendingError("INVALID_ARGUMENT", f"{label} must begin MCP_Bend and use only ASCII letters, digits, underscores or hyphens.")
    if (not isinstance(suppress_features, list) or not 1 <= len(suppress_features) <= 64
            or any(not isinstance(name, str) or not name or "\x00" in name or len(name) > 256 for name in suppress_features)
            or len(set(suppress_features)) != len(suppress_features) or feature_name in suppress_features):
        raise BendingError("INVALID_ARGUMENT", "suppress_features must list 1..64 unique exact original feature names.")
    model, path = _active(sw_app, document_path)
    part = _typed(model, "IPartDoc")
    features = _features(model)
    for name in suppress_features:
        if name not in features or features[name].GetTypeName2() != "Extrusion":
            raise BendingError("SOURCE_FEATURE_INVALID", f"Expected exact original extrusion {name!r}.")
    configs = list(model.GetConfigurationNames() or ())
    before_config = _config(model)
    if configuration in configs:
        owned_config = _typed(model.GetConfigurationByName(configuration), "IConfiguration")
        if str(owned_config.Description) != OWNER:
            raise BendingError("CONFIGURATION_NOT_OWNED", "Existing configuration lacks the MCP bend ownership marker.")
    old_feature = features.get(feature_name)
    if old_feature is not None:
        kind, marker = str(old_feature.GetTypeName2()), str(old_feature.Description)
        is_base, children = bool(old_feature.IsBase2()), old_feature.GetChildren() or ()
        # CreateFeatureFromBody3 reports BaseBody on SW2026 (live flat pilot),
        # while translated imports use Imported. Both still require our exact
        # marker, native base-feature status and zero dependent features.
        if kind not in {"Imported", "BaseBody"} or marker != OWNER or not is_base or children:
            raise BendingError("FEATURE_NOT_OWNED", "Existing feature must be our marked imported base with no dependent features; "
                               f"type={kind!r}, marker={marker!r}, is_base={is_base}, child_count={len(children)}.")
        if configuration not in configs:
            raise BendingError("CONFIGURATION_NOT_OWNED", "Owned feature exists without its requested owned configuration.")
        for name in configs:
            if name != configuration and not _suppressed(old_feature, name):
                raise BendingError("FEATURE_SCOPE_INVALID", f"Owned bend feature is active in another configuration: {name!r}.")
    # Expensive/native geometry construction and its volume proof precede all
    # configuration, feature, suppression and document mutations.
    temporary = _make_body(sw_app, data)
    temporary_evidence = _body_evidence(temporary, data["expected_volume_mm3"])
    result = {"ok": False, "document_path": str(path), "configuration": configuration,
              "feature": feature_name, "saved": False, "mutation_attempted": False,
              "geometry": {key: value for key, value in data.items() if key != "segments"},
              "temporary_body": temporary_evidence, "before_configuration": before_config,
              "note": "Analytic constant-thickness geometry with neutral factor0.5; not material forming simulation."}
    created_config = False
    created_feature = None
    old_copy = None
    old_evidence = None
    body_changed = False
    before_suppression = {}
    stage = "owned_configuration"
    try:
        if configuration not in configs:
            result["mutation_attempted"] = True
            manager = _typed(model.ConfigurationManager, "IConfigurationManager")
            made = manager.AddConfiguration2(configuration, OWNER, "", 0, "", OWNER, True)
            created_config = configuration in (model.GetConfigurationNames() or ())
            if made is None or not created_config:
                raise BendingError("CONFIGURATION_CREATE_FAILED", "AddConfiguration2 did not create the owned configuration.")
            if str(_typed(model.GetConfigurationByName(configuration), "IConfiguration").Description) != OWNER:
                raise BendingError("CONFIGURATION_CREATE_FAILED", "New configuration ownership marker readback differs.")
        if _config(model) != configuration:
            result["mutation_attempted"] = True
            if not model.ShowConfiguration2(configuration) or _config(model) != configuration:
                raise BendingError("CONFIGURATION_ACTIVATE_FAILED", "Could not activate the owned configuration.")
        features = _features(model)
        before_suppression = {name: _suppressed(features[name]) for name in suppress_features}
        if old_feature is not None:
            old_feature = features[feature_name]
            if _suppressed(old_feature):
                raise BendingError("FEATURE_SCOPE_INVALID", "Existing owned bend body is suppressed in its own configuration.")
            old_body = _body(part)
            old_evidence = _body_evidence(old_body)
            old_copy = _typed(old_body.Copy(), "IBody2")
            if old_copy is None:
                raise BendingError("ROLLBACK_UNAVAILABLE", "Cannot copy the existing body before replacement.")
        stage = "suppress_original_profiles"
        for name, was_suppressed in before_suppression.items():
            if not was_suppressed:
                result["mutation_attempted"] = True
                _suppress(features[name], True)
        if not model.EditRebuild3():
            raise BendingError("REBUILD_FAILED", "Suppression rebuild failed.")
        stage = "replace_owned_body" if old_feature is not None else "create_owned_body"
        result["mutation_attempted"] = True
        if old_feature is not None:
            body_changed = True
            if not old_feature.SetBody2(temporary, False):
                raise BendingError("BODY_REPLACE_FAILED", "SetBody2 rejected the replacement temporary body.")
            feature = old_feature
        else:
            if part.GetBodies2(0, False):
                raise BendingError("UNEXPECTED_SOLID", "Unlisted solid geometry remains; refusing to replace or merge it.")
            created_feature = _typed(part.CreateFeatureFromBody3(temporary, False, 1), "IFeature")
            if created_feature is None:
                raise BendingError("BODY_CREATE_FAILED", "CreateFeatureFromBody3 returned no feature.")
            feature = created_feature
            feature.Name = feature_name
            feature.Description = OWNER
            if str(feature.Name) != feature_name or str(feature.Description) != OWNER:
                raise BendingError("FEATURE_MARK_FAILED", "Cannot verify the owned feature name and marker.")
            for name in configs:
                if name != configuration:
                    _suppress(feature, True, name)
        stage = "native_body_readback"
        if not model.EditRebuild3():
            raise BendingError("REBUILD_FAILED", "Bend body rebuild failed.")
        evidence = _body_evidence(_body(part), data["expected_volume_mm3"])
        if any(abs(a - b) > 0.01 for a, b in zip(evidence["bounding_box_mm"], temporary_evidence["bounding_box_mm"])):
            raise BendingError("BOUNDS_MISMATCH", "Stored solid bounds differ from the verified temporary body.")
        if any(not _suppressed(_features(model)[name]) for name in suppress_features):
            raise BendingError("SUPPRESSION_FAILED", "An original profile became active unexpectedly.")
        if not _same_file(model.GetPathName(), path) or _config(model) != configuration:
            raise BendingError("DOCUMENT_MISMATCH", "Document/configuration changed during bending.")
        model.GraphicsRedraw2()
        result.update(ok=True, verified=True, configuration_created=created_config,
                      feature_created=created_feature is not None, measured=evidence,
                      rollback={"attempted": False})
        return result
    except Exception as exc:
        errors = []
        try:
            if configuration not in configs and configuration in (model.GetConfigurationNames() or ()):
                actual = _typed(model.GetConfigurationByName(configuration), "IConfiguration")
                if str(actual.Description) == OWNER:
                    created_config = True
                else:
                    errors.append("A newly created configuration lacks our marker; refusing to delete it.")
        except Exception as discovery_exc:
            errors.append(f"Cannot inspect partial configuration creation: {discovery_exc}")
        try:
            if _config(model) != configuration and configuration in (model.GetConfigurationNames() or ()):
                if not model.ShowConfiguration2(configuration):
                    raise RuntimeError("Cannot reactivate owned configuration for restoration.")
            if created_feature is not None:
                model.ClearSelection2(True)
                if not created_feature.Select2(False, 0) or not _typed(model.Extension, "IModelDocExtension").DeleteSelection2(0):
                    raise RuntimeError("Cannot remove newly created owned feature.")
                if feature_name in _features(model):
                    raise RuntimeError("New owned feature remains after removal.")
            elif body_changed and old_copy is not None:
                if not old_feature.SetBody2(old_copy, False) or not model.EditRebuild3():
                    raise RuntimeError("Cannot restore the prior body.")
                restored = _body_evidence(_body(part), old_evidence["volume_mm3"])
                if any(abs(a - b) > 0.001 for a, b in zip(restored["bounding_box_mm"], old_evidence["bounding_box_mm"])):
                    raise RuntimeError("Restored body bounds differ from the prior body.")
            for name, state in before_suppression.items():
                _suppress(_features(model)[name], state)
            if not model.EditRebuild3():
                raise RuntimeError("Restoration rebuild failed.")
        except Exception as restore_exc:
            errors.append(str(restore_exc))
        try:
            if _config(model) != before_config and not model.ShowConfiguration2(before_config):
                raise RuntimeError("Cannot restore the original active configuration.")
            if _config(model) != before_config:
                raise RuntimeError("Original active configuration readback differs.")
            if created_config:
                if not model.DeleteConfiguration2(configuration) or configuration in (model.GetConfigurationNames() or ()):
                    raise RuntimeError("Cannot remove newly created configuration.")
        except Exception as restore_exc:
            errors.append(str(restore_exc))
        try:
            remaining = set(_features(model)) - set(features)
            if remaining:
                errors.append(f"Unexpected new features remain after failure: {sorted(remaining)!r}.")
        except Exception as discovery_exc:
            errors.append(f"Cannot inspect remaining features: {discovery_exc}")
        result.update(code="SHEET_BEND_FAILED", failed={"stage": stage, "error": str(exc)},
                      partial_state_possible=bool(errors), configuration_created=created_config,
                      rollback={"attempted": result["mutation_attempted"], "verified": not errors, "errors": errors})
        return result
