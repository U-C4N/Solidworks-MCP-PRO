"""Native scene/HDRI controls for one explicitly identified writable CAD document.

Call only on the existing CAD worker while holding its writer lease. No new CAD
instance, native UI automation, document Save, or claimed rollback is used.
Scene values are verified in SOLIDWORKS; their Visualize transfer still requires
an exported project/render to be inspected by the caller.

API references (installed SOLIDWORKS 2026 type library also inspected):
https://help.solidworks.com/2026/English/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.ISwScene_members.html
https://help.solidworks.com/2016/english/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.ISwScene~EnvironmentRotation.html
https://help.solidworks.com/2026/English/api/swconst/SolidWorks.Interop.swconst~SolidWorks.Interop.swconst.swSceneBackgroundType_e.html
https://help.solidworks.com/2022/english/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.IModelDoc2~LightSourcePropertyValues.html
"""

from __future__ import annotations

import math
import os
import stat
import struct
from array import array
from pathlib import Path
from typing import Any


class SceneError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _typed(obj: Any, interface: str) -> Any:
    if obj is None or not hasattr(obj, "_oleobj_"):
        return obj
    from .native import binding

    return binding.wrap(obj, getattr(binding.module(), interface))


def _file(value: Any, suffixes: tuple[str, ...], label: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value) or "\x00" in str(value):
        raise SceneError("INVALID_ARGUMENT", f"{label} must name an absolute existing local file.")
    path = Path(value)
    if (not path.is_absolute() or str(path).startswith(("\\\\", "//"))
            or path.suffix.lower() not in suffixes or path.is_symlink()
            or not path.is_file() or path.stat().st_size == 0):
        raise SceneError("INVALID_ARGUMENT", f"{label} must be an existing local {suffixes} file: {value!r}.")
    return path


def _same_file(actual: str, expected: str | Path) -> bool:
    try:
        return os.path.samefile(actual, expected)
    except (OSError, ValueError):
        return False


def _scene(model: Any) -> Any:
    config = _typed(model.GetActiveConfiguration(), "IConfiguration")
    if config is None:
        raise RuntimeError("No active configuration.")
    return _typed(config.GetScene(), "ISwScene")


_PROPERTIES = {
    "hdri_path": "BackgroundEnvImage",
    "background_type": "BackgroundType",
    "environment_rotation_deg": "EnvironmentRotation",
    "floor_shadows": "FloorShadows",
    "floor_reflections": "FloorReflections",
    "rendering_brightness": "RenderingBrightness",
    "background_top_rgb": "BackgroundTopGradientColor",
    "background_bottom_rgb": "BackgroundBottomGradientColor",
}

BACKGROUND_TYPES = {"plain": 1, "gradient": 2, "environment": 4}


def _rgb(value: Any, label: str) -> list[int]:
    if (not isinstance(value, (list, tuple)) or len(value) != 3
            or any(type(channel) is not int or not 0 <= channel <= 255 for channel in value)):
        raise SceneError("INVALID_ARGUMENT", f"{label} must contain three integers in 0..255.")
    return list(value)


def _scene_snapshot(scene: Any) -> dict:
    if scene is None:
        return {"available": False}
    result = {"available": True}
    for name, native in _PROPERTIES.items():
        value = getattr(scene, native)
        result[name] = [(int(value) >> shift) & 255 for shift in (0, 8, 16)] if name.endswith("_rgb") else value
    result["background_type_name"] = next(
        (name for name, value in BACKGROUND_TYPES.items() if value == result["background_type"]), None
    )
    try:
        value = scene.GetP2SFileName("")
        if isinstance(value, (tuple, list)) and len(value) == 1:
            value = value[0]
        if not isinstance(value, str):
            raise RuntimeError(f"Unexpected GetP2SFileName result: {value!r}.")
        result["scene_path"] = value
    except Exception as exc:
        result["scene_path_read_error"] = str(exc)
    return result


def _light_values(model: Any, index: int) -> list[float]:
    values = model.LightSourcePropertyValues(index)
    if (not isinstance(values, (list, tuple, array)) or len(values) != 19
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in values)):
        raise RuntimeError(f"Light {index} did not return the documented 19 finite doubles.")
    return [float(value) for value in values]


def _packed_integer(value: float) -> int:
    # SOLIDWORKS documents type and isDisabled as integers packed in a double.
    # Do not int(value): 1 packed into an IEEE double is approximately 5e-324.
    return struct.unpack("<ii", struct.pack("<d", value))[0]


def _set_light_values(model: Any, index: int, values: list[float]) -> None:
    data: Any = list(values)
    if hasattr(model, "_oleobj_"):
        import pythoncom
        import win32com.client

        # IModelDoc2 accepts a VARIANT but interprets its payload as R8[19].
        # A Python sequence/array can marshal as SAFEARRAY(VARIANT), corrupting
        # native values even though the call returns without a COM exception.
        data = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, data)
    model.SetLightSourcePropertyValues(index, data)


def _lights(model: Any) -> list[dict]:
    count = model.GetLightSourceCount()
    if type(count) is not int or not 0 <= count <= 256:
        raise RuntimeError(f"Unexpected light count: {count!r}.")
    result = []
    extension = _typed(model.Extension, "IModelDocExtension")
    for index in range(count):
        values = _light_values(model, index)
        result.append({"id": index, "name": str(model.GetLightSourceName(index)),
                       "native_type": _packed_integer(values[0]),
                       "disabled": bool(_packed_integer(values[17])),
                       "render_enabled": bool(extension.GetLightEnabledInRender(index)),
                       "intensity": values[1], "ambient": values[15], "specular": values[16],
                       "values": values})
    return result


def _targets(lights: list[dict], names: list[str], ids: list[int] | None = None) -> list[dict]:
    result = []
    for name in names:
        matches = [light for light in lights if light["name"] == name]
        if len(matches) != 1:
            raise SceneError("LIGHT_NOT_UNIQUE", f"Expected exactly one light named {name!r}; found {len(matches)}.")
        result.append(matches[0])
    for index in ids or []:
        matches = [light for light in lights if light["id"] == index]
        if len(matches) != 1:
            raise SceneError("LIGHT_ID_UNKNOWN", f"Expected an existing light ID {index}; found {len(matches)}.")
        result.append(matches[0])
    return result


def _matches(name: str, actual: Any, requested: Any) -> bool:
    if name == "hdri_path":
        return _same_file(str(actual), requested)
    if type(requested) is bool:
        return type(actual) is bool and actual == requested
    if isinstance(requested, float):
        return type(actual) in (int, float) and math.isclose(actual, requested, rel_tol=1e-7, abs_tol=1e-7)
    return actual == requested


def apply(
    sw_app: Any,
    document_path: str | Path,
    scene_path: str | Path | None = None,
    *,
    hdri_path: str | Path | None = None,
    environment_rotation_deg: float | None = None,
    floor_shadows: bool | None = None,
    floor_reflections: bool | None = None,
    rendering_brightness: float | None = None,
    background_type: str | None = None,
    background_top_rgb: list[int] | None = None,
    background_bottom_rgb: list[int] | None = None,
    disable_light_names: list[str] | None = None,
    disable_light_ids: list[int] | None = None,
    dry_run: bool = False,
) -> dict:
    """Inspect or edit native scene on the exact active saved part/assembly.

    Rotation uses degrees, 0..360. HDR paths select swBackgroundType_UseEnvironment
    (4), unless background_type overrides the backdrop after the HDR is set.
    RenderingBrightness is a native setting documented historically for PhotoView
    360. Its Visualize effect requires a render comparison; native readback alone
    is not evidence of a changed rendered image. Local bounds are 0.1..5.
    Light disabling changes the packed disabled flag and native render-enable
    flag, never deletes lights. IDs are useful when native names are duplicated;
    IDs refer to the current inventory and cannot be combined with scene import.
    ``saved`` means document Save was not called; ISwScene.Modify commits scene
    edits into the model's scene. No part references are opened or changed.
    """
    path = _file(document_path, (".sldasm", ".sldprt"), "document_path")
    source_scene = _file(scene_path, (".p2s",), "scene_path") if scene_path is not None else None
    hdr = _file(hdri_path, (".hdr", ".exr"), "hdri_path") if hdri_path is not None else None
    for label, value in (("dry_run", dry_run), ("floor_shadows", floor_shadows),
                         ("floor_reflections", floor_reflections)):
        if value is not None and type(value) is not bool:
            raise SceneError("INVALID_ARGUMENT", f"{label} must be boolean.")
    if type(dry_run) is not bool:
        raise SceneError("INVALID_ARGUMENT", "dry_run must be boolean.")
    if environment_rotation_deg is not None:
        if (type(environment_rotation_deg) not in (int, float)
                or not math.isfinite(environment_rotation_deg) or not 0 <= environment_rotation_deg <= 360):
            raise SceneError("INVALID_ARGUMENT", "environment_rotation_deg must be finite and within 0..360.")
    if rendering_brightness is not None and (
        type(rendering_brightness) not in (int, float) or not math.isfinite(rendering_brightness)
        or not 0.1 <= rendering_brightness <= 5
    ):
        raise SceneError("INVALID_ARGUMENT", "rendering_brightness must be finite and within 0.1..5.")
    if background_type is not None and (not isinstance(background_type, str) or background_type not in BACKGROUND_TYPES):
        raise SceneError("INVALID_ARGUMENT", "background_type must be environment, plain, or gradient.")
    top_rgb = _rgb(background_top_rgb, "background_top_rgb") if background_top_rgb is not None else None
    bottom_rgb = _rgb(background_bottom_rgb, "background_bottom_rgb") if background_bottom_rgb is not None else None
    if floor_reflections is True and floor_shadows is False:
        raise SceneError("INVALID_ARGUMENT", "Floor reflections require floor shadows.")
    names = [] if disable_light_names is None else disable_light_names
    if (not isinstance(names, list) or len(names) > 64
            or any(not isinstance(name, str) or not name.strip() or "\x00" in name for name in names)
            or len(set(names)) != len(names)):
        raise SceneError("INVALID_ARGUMENT", "disable_light_names must contain up to 64 unique exact nonempty names.")
    ids = [] if disable_light_ids is None else disable_light_ids
    if (not isinstance(ids, list) or len(ids) > 64
            or any(type(index) is not int or not 0 <= index <= 255 for index in ids)
            or len(set(ids)) != len(ids) or (ids and names)):
        raise SceneError("INVALID_ARGUMENT", "disable_light_ids must contain up to 64 unique integers 0..255, mutually exclusive with names.")
    if ids and source_scene:
        raise SceneError("INVALID_ARGUMENT", "Insert the scene first, then inspect and disable its current light IDs in a separate call.")
    requested = {}
    if hdr is not None:
        requested.update(background_type=4, hdri_path=str(hdr))
    if background_type is not None:
        requested["background_type"] = BACKGROUND_TYPES[background_type]
    if top_rgb is not None:
        requested["background_top_rgb"] = top_rgb
    if bottom_rgb is not None:
        requested["background_bottom_rgb"] = bottom_rgb
    if rendering_brightness is not None:
        requested["rendering_brightness"] = float(rendering_brightness)
    if environment_rotation_deg is not None:
        requested["environment_rotation_deg"] = float(environment_rotation_deg)
    if floor_shadows is not None:
        requested["floor_shadows"] = floor_shadows
    if floor_reflections is not None:
        requested["floor_reflections"] = floor_reflections
    stage = "active_document"
    try:
        model = _typed(sw_app.ActiveDoc, "IModelDoc2")
        if model is None:
            raise SceneError("NO_ACTIVE_DOCUMENT", "No active saved document.")
        stage = "document_identity"
        if not _same_file(str(model.GetPathName()), path):
            raise SceneError("DOCUMENT_MISMATCH", "Active document does not match document_path.")
        if model.GetType() not in (1, 2):
            raise SceneError("MODEL_REQUIRED", "A saved part or assembly is required.")
        if (model.IsOpenedReadOnly() or model.IsOpenedViewOnly()
                or not path.stat().st_mode & stat.S_IWRITE):
            raise SceneError("DOCUMENT_READ_ONLY", "Target document is read-only or view-only.")
        stage = "scene_and_light_readback"
        extension = _typed(model.Extension, "IModelDocExtension")
        before = {"scene": _scene_snapshot(_scene(model)), "lights": _lights(model),
                  "realview_enabled": bool(extension.ViewDisplayRealView)}
        _targets(before["lights"], names, ids)
        effective_background = requested.get("background_type", before["scene"].get("background_type"))
        if (top_rgb is not None or bottom_rgb is not None) and source_scene and background_type is None:
            raise SceneError("INVALID_ARGUMENT", "Colors with scene_path require explicit background_type.")
        if top_rgb is not None and effective_background not in (1, 2):
            raise SceneError("INVALID_ARGUMENT", "background_top_rgb requires plain or gradient background_type.")
        if bottom_rgb is not None and effective_background != 2:
            raise SceneError("INVALID_ARGUMENT", "background_bottom_rgb requires gradient background_type.")
        if not before["scene"]["available"] and source_scene is None:
            raise SceneError("SCENE_UNAVAILABLE", "Current configuration has no scene; supply scene_path.")
        if (floor_reflections is True and floor_shadows is not True and source_scene is None
                and not before["scene"].get("floor_shadows")):
            raise SceneError("INVALID_ARGUMENT", "Explicit floor_shadows=true is required before enabling reflections.")
    except SceneError:
        raise
    except Exception as exc:
        raise SceneError("SCENE_PREFLIGHT_FAILED", f"{stage}: {exc}") from exc

    base = {"document_path": str(path), "configuration_scope": "active", "saved": False,
            "rollback_performed": False, "before": before,
            "requested": {"scene_path": str(source_scene) if source_scene else None,
                          **requested, "disable_light_names": names, "disable_light_ids": ids},
            "visualize_transfer_verified": False}
    if rendering_brightness is not None:
        base["rendering_brightness_scope"] = "native_ISwScene_setting; Visualize_effect_requires_render_comparison"
    if dry_run or not (source_scene or requested or names or ids):
        return {**base, "ok": True, "dry_run": dry_run, "mutation_attempted": False}

    changed = False
    restore_realview = False
    result = {**base, "ok": False, "mutation_attempted": False, "disabled_lights": []}
    try:
        if source_scene:
            stage = "enable_realview_for_insert"
            if not before["realview_enabled"]:
                restore_realview = True
                extension.ViewDisplayRealView = True
                if not extension.ViewDisplayRealView:
                    raise RuntimeError("SOLIDWORKS could not enable RealView required by InsertScene.")
            stage = "insert_scene"
            changed = True
            accepted = extension.InsertScene(str(source_scene))
            result["insert_scene_accepted"] = bool(accepted)
            if not accepted:
                raise RuntimeError("InsertScene returned false.")
            stage = "insert_scene_readback"
            inserted = _scene_snapshot(_scene(model))
            result["inserted_scene"] = inserted
            if not _same_file(inserted.get("scene_path", ""), source_scene):
                raise RuntimeError(f"Inserted scene file identity could not be verified: {inserted!r}.")
        stage = "scene_settings"
        scene = _scene(model)
        if scene is None:
            raise RuntimeError("No scene after insertion.")
        if floor_reflections is True and floor_shadows is not True and not scene.FloorShadows:
            raise RuntimeError("Selected scene has no floor shadows; reflections require explicit floor_shadows=true.")
        settings = list(requested.items())
        if hdr is not None:
            # BackgroundEnvImage is documented as valid only in environment
            # mode. Set the HDR there, then select the requested plain/gradient
            # backdrop; the final native readback must retain the HDR path.
            settings = [("background_type", 4), ("hdri_path", str(hdr)),
                        ("background_type", requested["background_type"])] + [
                (name, value) for name, value in settings if name not in ("background_type", "hdri_path")
            ]
        # SW2026 live evidence: clearing FloorReflections also cleared
        # FloorShadows. Clear reflections first, then establish requested shadow
        # state. Enabling reflections still follows enabling shadows.
        if requested.get("floor_reflections") is False:
            settings = [("floor_reflections", False)] + [
                (name, value) for name, value in settings if name != "floor_reflections"
            ]
        for name, value in settings:
            changed = True
            if name.endswith("_rgb"):
                value = value[0] | (value[1] << 8) | (value[2] << 16)
            setattr(scene, _PROPERTIES[name], value)
        if requested:
            stage = "commit_scene"
            result["scene_before_commit"] = _scene_snapshot(scene)
            if not scene.Modify():
                raise RuntimeError("ISwScene.Modify returned false.")
        stage = "disable_lights"
        current_targets = _targets(_lights(model), names, ids)
        for light in current_targets:
            if ids:
                prior = next(item for item in before["lights"] if item["id"] == light["id"])
                if (prior["name"], prior["native_type"]) != (light["name"], light["native_type"]):
                    raise RuntimeError(f"Light identity changed since preflight for ID {light['id']}.")
            values = light["values"].copy()
            # Preserve the unused high 32 bits rather than rewriting the whole slot.
            high = struct.unpack("<ii", struct.pack("<d", values[17]))[1]
            values[17] = struct.unpack("<d", struct.pack("<ii", 1, high))[0]
            changed = True
            _set_light_values(model, light["id"], values)
            extension.SetLightEnabledInRender(light["id"], False)
            actual = _light_values(model, light["id"])
            if not _packed_integer(actual[17]):
                raise RuntimeError(f"Light {light['name']!r} is still enabled.")
            if extension.GetLightEnabledInRender(light["id"]):
                raise RuntimeError(f"Light ID {light['id']} is still enabled in the native render scene.")
            if any(struct.pack("<d", actual[i]) != struct.pack("<d", values[i]) for i in range(19) if i != 17):
                raise RuntimeError(f"Disabling light {light['name']!r} unexpectedly changed other light properties.")
            result["disabled_lights"].append({"id": light["id"], "name": light["name"], "verified": True})
        stage = "redraw_and_readback"
        model.GraphicsRedraw2()
        after = {"scene": _scene_snapshot(_scene(model)), "lights": _lights(model)}
        result["after"] = after
        if "background_type" in requested and hdr is None and before["scene"].get("hdri_path"):
            if not _same_file(str(after["scene"].get("hdri_path", "")), before["scene"]["hdri_path"]):
                raise RuntimeError("Changing backdrop did not preserve the existing HDRI file readback.")
        mismatches = {name: {"requested": value, "actual": after["scene"].get(name)}
                      for name, value in requested.items()
                      if not _matches(name, after["scene"].get(name), value)}
        if mismatches:
            raise RuntimeError(f"Native scene readback mismatch: {mismatches!r}.")
        if any(not item["disabled"] or item["render_enabled"] for item in _targets(after["lights"], names, ids)):
            raise RuntimeError("A requested disabled light became enabled again.")
        result.update(ok=True, verified=True)
    except Exception as exc:
        result.update(code="SCENE_APPLY_FAILED", failed={"stage": stage, "error": str(exc)},
                      partial_state_possible=changed)
        try:
            result["after_failure"] = {"scene": _scene_snapshot(_scene(model)), "lights": _lights(model)}
        except Exception as read_exc:
            result["failure_readback_error"] = str(read_exc)
    finally:
        result["mutation_attempted"] = changed
        if restore_realview:
            try:
                extension.ViewDisplayRealView = before["realview_enabled"]
                result["realview_restored"] = bool(extension.ViewDisplayRealView) == before["realview_enabled"]
                if not result["realview_restored"]:
                    raise RuntimeError("RealView preference restoration readback differs.")
            except Exception as exc:
                result.update(ok=False, realview_restore_error=str(exc), partial_state_possible=True)
    return result
