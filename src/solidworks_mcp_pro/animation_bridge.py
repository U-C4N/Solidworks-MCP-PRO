"""Guarded native assembly preparation for frame-by-frame animation.

Runs only on the existing CAD worker under its writer lease. Inspection is
read-only; configuration changes never save assembly or referenced part files.
Reacquire components after each configuration change because SOLIDWORKS
invalidates component traversal arrays when referenced configurations change.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any


class AnimationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _typed(obj: Any, interface: str) -> Any:
    if obj is None or not hasattr(obj, "_oleobj_"):
        return obj
    from .native import binding

    return binding.wrap(obj, getattr(binding.module(), interface))


def _same_file(left: str | Path, right: str | Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except (OSError, ValueError):
        return False


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value or len(value) > 1024:
        raise AnimationError("INVALID_ARGUMENT", f"{label} must be an exact nonempty name of at most 1024 characters.")
    return value


def _active(sw_app: Any, document_path: str | Path, writable: bool) -> tuple[Any, Path]:
    if not isinstance(document_path, (str, Path)) or not str(document_path) or "\x00" in str(document_path):
        raise AnimationError("INVALID_ARGUMENT", "document_path must name an existing absolute local assembly.")
    path = Path(document_path)
    if (not path.is_absolute() or str(path).startswith(("\\\\", "//")) or path.is_symlink()
            or path.suffix.lower() != ".sldasm" or not path.is_file() or not path.stat().st_size):
        raise AnimationError("INVALID_ARGUMENT", "document_path must name an existing absolute local .sldasm file.")
    model = _typed(sw_app.ActiveDoc, "IModelDoc2")
    if model is None:
        raise AnimationError("NO_ACTIVE_DOCUMENT", "No active saved assembly.")
    if not _same_file(str(model.GetPathName()), path):
        raise AnimationError("DOCUMENT_MISMATCH", "The active saved document does not match document_path.")
    if model.GetType() != 2:
        raise AnimationError("ASSEMBLY_REQUIRED", "The target must be an assembly.")
    if model.IsOpenedViewOnly():
        raise AnimationError("DOCUMENT_VIEW_ONLY", "View-only assemblies are unsupported.")
    if writable and (model.IsOpenedReadOnly() or not path.stat().st_mode & stat.S_IWRITE):
        raise AnimationError("DOCUMENT_READ_ONLY", "The target assembly is read-only.")
    return model, path


def _configuration(model: Any) -> str:
    config = _typed(model.GetActiveConfiguration(), "IConfiguration")
    if config is None:
        raise RuntimeError("No active assembly configuration.")
    return str(config.Name)


def _configuration_names(owner: Any) -> list[str]:
    values = owner.GetConfigurationNames()
    if not isinstance(values, (tuple, list)) or any(not isinstance(value, str) or not value for value in values):
        raise RuntimeError(f"Unexpected native configuration list: {values!r}.")
    return list(values)


def _component(model: Any, name: str) -> Any:
    assembly = _typed(model, "IAssemblyDoc")
    matches = []
    for raw in assembly.GetComponents(True) or ():
        item = _typed(raw, "IComponent2")
        if str(item.Name2) == name:
            matches.append(item)
    if len(matches) != 1:
        raise AnimationError("COMPONENT_NOT_UNIQUE", f"Expected one top-level component named {name!r}; found {len(matches)}.")
    return matches[0]


def _record(item: Any) -> dict:
    result = {"component": str(item.Name2), "document_path": str(item.GetPathName()),
              "referenced_configuration": str(item.ReferencedConfiguration)}
    try:
        result["configurations"] = _configuration_names(item)
    except Exception as exc:
        result["configurations_error"] = str(exc)
    return result


def _inspect(model: Any, path: Path) -> dict:
    assembly = _typed(model, "IAssemblyDoc")
    top_names = {str(_typed(item, "IComponent2").Name2) for item in assembly.GetComponents(True) or ()}
    records = []
    for raw in assembly.GetComponents(False) or ():
        item = _typed(raw, "IComponent2")
        record = _record(item)
        record["is_top_level"] = record["component"] in top_names
        records.append(record)
    return {"document_path": str(path), "configuration": _configuration(model),
            "configurations": _configuration_names(model), "read_only": bool(model.IsOpenedReadOnly()),
            "components": records, "component_count": len(records)}


def inspect(sw_app: Any, document_path: str | Path) -> dict:
    """Inspect active exact saved assembly; read-only assemblies are accepted."""
    model, path = _active(sw_app, document_path, writable=False)
    return {"ok": True, "mutation_attempted": False, "saved": False, **_inspect(model, path)}


def _rebuild(model: Any) -> None:
    if not model.EditRebuild3():
        raise RuntimeError("SOLIDWORKS EditRebuild3 returned false.")


def configure(
    sw_app: Any,
    document_path: str | Path,
    *,
    configuration: str | None = None,
    components: list[dict] | None = None,
) -> dict:
    """Switch assembly configuration OR exact top-level component configurations.

    components entries contain only ``component`` and ``configuration``. Changing
    an assembly configuration can replace the component inventory, so perform
    that operation separately and inspect again before component assignments.
    Failures attempt restoration and report whether readbacks prove it succeeded.
    """
    if configuration is not None:
        _name(configuration, "configuration")
    assignments = [] if components is None else components
    if not isinstance(assignments, list) or len(assignments) > 128 or (configuration is not None and assignments):
        raise AnimationError("INVALID_ARGUMENT", "Use configuration OR a list of at most 128 component assignments.")
    if configuration is None and not assignments:
        raise AnimationError("INVALID_ARGUMENT", "Specify an assembly configuration or component assignments.")
    names = set()
    for assignment in assignments:
        if not isinstance(assignment, dict) or set(assignment) != {"component", "configuration"}:
            raise AnimationError("INVALID_ARGUMENT", "Each assignment requires exactly component and configuration.")
        name = _name(assignment["component"], "component")
        _name(assignment["configuration"], "component configuration")
        if name in names:
            raise AnimationError("INVALID_ARGUMENT", f"Duplicate component assignment: {name!r}.")
        names.add(name)
    model, path = _active(sw_app, document_path, writable=True)
    before_configuration = _configuration(model)
    if configuration is not None and configuration not in _configuration_names(model):
        raise AnimationError("CONFIGURATION_UNKNOWN", f"Assembly configuration not found: {configuration!r}.")
    before_components = {}
    for assignment in assignments:
        item = _component(model, assignment["component"])
        if assignment["configuration"] not in _configuration_names(item):
            raise AnimationError("CONFIGURATION_UNKNOWN", f"Component {assignment['component']!r} has no configuration {assignment['configuration']!r}.")
        before_components[assignment["component"]] = _record(item)
        del item
    result = {"ok": False, "document_path": str(path), "saved": False,
              "scope": "active_assembly_configuration", "before_configuration": before_configuration,
              "before_components": list(before_components.values()), "applied": [], "mutation_attempted": False}
    changed_names = []
    stage = "activate_configuration"
    try:
        if configuration is not None and configuration != before_configuration:
            result["mutation_attempted"] = True
            if not model.ShowConfiguration2(configuration):
                raise RuntimeError("ShowConfiguration2 returned false.")
            _rebuild(model)
            if _configuration(model) != configuration:
                raise RuntimeError("Active configuration readback differs from the requested name.")
        for assignment in assignments:
            name = assignment["component"]
            stage = f"component_configuration:{name}"
            item = _component(model, name)
            prior = before_components[name]
            if not _same_file(str(item.GetPathName()), prior["document_path"]):
                raise RuntimeError(f"Referenced file identity changed for {name!r}.")
            if str(item.ReferencedConfiguration) != assignment["configuration"]:
                result["mutation_attempted"] = True
                changed_names.append(name)
                item.ReferencedConfiguration = assignment["configuration"]
                del item
                _rebuild(model)
            else:
                del item
            actual = _record(_component(model, name))
            if actual["referenced_configuration"] != assignment["configuration"]:
                raise RuntimeError(f"Referenced configuration readback differs for {name!r}: {actual!r}.")
            result["applied"].append(actual)
        stage = "final_readback"
        expected_configuration = configuration if configuration is not None else before_configuration
        if _configuration(model) != expected_configuration:
            raise RuntimeError("Assembly configuration changed unexpectedly.")
        for assignment in assignments:
            actual = _component(model, assignment["component"])
            if str(actual.ReferencedConfiguration) != assignment["configuration"]:
                raise RuntimeError(f"Later mutation changed {assignment['component']!r} configuration.")
            del actual
        model.GraphicsRedraw2()
        result.update(ok=True, verified=True, configuration=_configuration(model), rollback={"attempted": False})
        return result
    except Exception as exc:
        errors = []
        for name in reversed(changed_names):
            try:
                item = _component(model, name)
                prior = before_components[name]
                if not _same_file(str(item.GetPathName()), prior["document_path"]):
                    raise RuntimeError("Referenced file identity changed; refusing to restore another component file.")
                item.ReferencedConfiguration = prior["referenced_configuration"]
                del item
                _rebuild(model)
            except Exception as rollback_exc:
                errors.append(f"{name}: {rollback_exc}")
        try:
            if _configuration(model) != before_configuration:
                if not model.ShowConfiguration2(before_configuration):
                    raise RuntimeError("Original assembly configuration activation failed.")
                _rebuild(model)
            if _configuration(model) != before_configuration:
                raise RuntimeError("Original assembly configuration readback differs.")
            for name, prior in before_components.items():
                item = _component(model, name)
                if str(item.ReferencedConfiguration) != prior["referenced_configuration"]:
                    raise RuntimeError(f"Restoration readback differs for {name!r}.")
                del item
            model.GraphicsRedraw2()
        except Exception as rollback_exc:
            errors.append(str(rollback_exc))
        result.update(code="CONFIGURATION_CHANGE_FAILED", failed={"stage": stage, "error": str(exc)},
                      partial_state_possible=bool(errors),
                      rollback={"attempted": bool(result["mutation_attempted"]), "verified": not errors, "errors": errors})
        return result


set_configuration = configure


def _out_tuple(value: Any, length: int, label: str) -> tuple:
    if not isinstance(value, (tuple, list)) or len(value) != length or value[0] is not True:
        raise AnimationError("PACK_AND_GO_PREFLIGHT_FAILED", f"{label} returned an unexpected or unsuccessful value: {value!r}.")
    return tuple(value)


def _pack_call(label: str, fn: Any, *args: Any) -> Any:
    try:
        return fn(*args)
    except Exception as exc:
        raise AnimationError("PACK_AND_GO_PREFLIGHT_FAILED", f"{label}: {exc}") from exc


def _pack_objects(sw_app: Any, model: Any, source_path: Path) -> tuple[Any, Any, bool]:
    """Use the typed vtable, not IDispatch.Invoke, for native Pack and Go.

    SW2026's GetPackAndGo InvokeTypes call failed DISP_E_PARAMNOTOPTIONAL even
    after explicit QI, although the typed .NET call succeeded on the same file.
    The installed typelib declares an IPackAndGo** retval and SavePackAndGo
    takes an IPackAndGo*. Keep both sides on that typed comtypes contract.
    Reattach only to the existing ROT object on this SAME CAD-worker STA;
    never create an instance. Recheck PID, document and saved state before the
    first package call because ROT can select a different SOLIDWORKS instance.
    """
    if not hasattr(sw_app, "_oleobj_"):
        extension = model.Extension
        return extension, _pack_call("GetPackAndGo", extension.GetPackAndGo), False
    import comtypes.client
    from .native import binding

    installed = binding.module()
    module = _pack_call("load registered SOLIDWORKS typelib", comtypes.client.GetModule,
                        (str(installed.CLSID), installed.MajorVersion, installed.MinorVersion))
    app = _pack_call("attach existing SOLIDWORKS vtable", comtypes.client.GetActiveObject,
                     "SldWorks.Application", module.ISldWorks)
    if int(app.GetProcessID()) != int(sw_app.GetProcessID()):
        raise AnimationError("SOLIDWORKS_INSTANCE_MISMATCH", "Pack and Go ROT connection differs from the CAD session.")
    raw_model = app.ActiveDoc
    if not raw_model:
        raise AnimationError("NO_ACTIVE_DOCUMENT", "The existing SOLIDWORKS instance has no active document.")
    native_model = _pack_call("QueryInterface(IModelDoc2)", raw_model.QueryInterface, module.IModelDoc2)
    if not _same_file(str(native_model.GetPathName()), source_path):
        raise AnimationError("DOCUMENT_MISMATCH", "Pack and Go vtable connection targets another active document.")
    if native_model.GetType() != 2:
        raise AnimationError("ASSEMBLY_REQUIRED", "The vtable target must be an assembly.")
    if native_model.IsOpenedViewOnly():
        raise AnimationError("DOCUMENT_VIEW_ONLY", "View-only assemblies are unsupported.")
    if native_model.GetSaveFlag():
        raise AnimationError("SOURCE_UNSAVED", "Source changed before Pack and Go; save it explicitly and retry.")
    extension = native_model.Extension
    if not extension:
        raise AnimationError("PACK_AND_GO_UNAVAILABLE", "SOLIDWORKS returned no model extension.")
    return extension, _pack_call("vtable GetPackAndGo", extension.GetPackAndGo), True


def _pack_outputs(label: str, fn: Any, out_count: int, vtable: bool) -> tuple:
    """Normalize verified native output order without treating BOOL as HRESULT.

    comtypes returns [out] parameters in declaration order, including the
    trailing [out, retval] BOOL. makepy returns BOOL first and requires explicit
    placeholders for these nonoptional out arguments. Test doubles exercise
    both contracts; production Pack and Go always uses the vtable branch.
    """
    value = _pack_call(label, fn) if vtable else _pack_call(label, fn, *([None] * out_count))
    if vtable and isinstance(value, (tuple, list)) and len(value) == out_count + 1:
        value = (value[-1], *value[:-1])
    return _out_tuple(value, out_count + 1, label)


def _new_directory(value: Any) -> Path:
    if not isinstance(value, (str, Path)) or not str(value) or "\x00" in str(value):
        raise AnimationError("INVALID_ARGUMENT", "output_directory must be a new absolute local directory.")
    path = Path(value)
    if (not path.is_absolute() or str(path).startswith(("\\\\", "//")) or os.path.lexists(path)
            or not path.parent.is_dir()):
        raise AnimationError("OUTPUT_EXISTS_OR_INVALID", "output_directory must not exist and its local parent must exist.")
    for parent in (path.parent, *path.parents):
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise AnimationError("OUTPUT_LINKED", "Linked output directory ancestors are not supported.")
    return path.resolve()


def _inside_file(path: Path, directory: Path) -> bool:
    try:
        return path.resolve().is_relative_to(directory.resolve()) and path.resolve() != directory.resolve()
    except (OSError, ValueError):
        return False


def clone(
    sw_app: Any,
    document_path: str | Path,
    output_directory: str | Path,
    *,
    prefix: str = "Animation_",
) -> dict:
    """Native Pack and Go of a saved assembly and its complete CAD references.

    Writes only a newly claimed directory, flattens with collision checks, adds
    a filename prefix, includes suppressed/toolbox components, never auto-opens
    the copy or saves the source. Native save statuses and dependency paths of
    the saved assembly must prove isolation before reporting success.
    """
    target = _new_directory(output_directory)
    if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", prefix):
        raise AnimationError("INVALID_ARGUMENT", "prefix must contain 1..48 ASCII letters, digits, underscores or hyphens.")
    model, source_path = _active(sw_app, document_path, writable=False)
    if model.GetSaveFlag():
        raise AnimationError("SOURCE_UNSAVED", "Save the source assembly explicitly before cloning; this tool never saves it.")
    extension, package, vtable = _pack_objects(sw_app, model, source_path)
    if not package:
        raise AnimationError("PACK_AND_GO_UNAVAILABLE", "SOLIDWORKS returned no Pack and Go object.")
    options = {"IncludeDrawings": False, "IncludeSimulationResults": False,
               "IncludeSuppressed": True, "IncludeToolboxComponents": True,
               "FlattenToSingleFolder": True, "AddPrefix": prefix, "AddSuffix": ""}
    for name, value in options.items():
        _pack_call("set " + name, setattr, package, name, value)
        if _pack_call("get " + name, getattr, package, name) != value:
            raise AnimationError("PACK_AND_GO_PREFLIGHT_FAILED", f"Native option {name} did not accept the requested value.")
    _, raw_sources = _pack_outputs("GetDocumentNames", package.GetDocumentNames, 1, vtable)
    if (not isinstance(raw_sources, (tuple, list)) or not raw_sources
            or len(raw_sources) != package.GetDocumentNamesCount()):
        raise AnimationError("PACK_AND_GO_PREFLIGHT_FAILED", "Document count and source path list differ.")
    sources = []
    destinations = []
    seen_sources = set()
    seen_destinations = set()
    source_metadata = {}
    for raw in raw_sources:
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise AnimationError("PACK_AND_GO_PREFLIGHT_FAILED", "Pack and Go returned a null or invalid source path.")
        source = Path(raw)
        if (not source.is_absolute() or str(source).startswith(("\\\\", "//"))
                or source.is_symlink() or not source.is_file() or not source.stat().st_size):
            raise AnimationError("PACK_AND_GO_SOURCE_UNAVAILABLE", f"Missing, virtual, remote, empty or linked source: {raw!r}.")
        source_key = os.path.normcase(str(source.resolve()))
        destination = target / (prefix + source.name)
        destination_key = os.path.normcase(str(destination))
        if source_key in seen_sources or destination_key in seen_destinations:
            raise AnimationError("PACK_AND_GO_NAME_COLLISION", "Flattened source names collide; choose a source assembly without basename collisions.")
        seen_sources.add(source_key)
        seen_destinations.add(destination_key)
        sources.append(source)
        destinations.append(destination)
        source_metadata[str(source)] = (source.stat().st_size, source.stat().st_mtime_ns)
    assembly_indices = [index for index, source in enumerate(sources) if _same_file(source, source_path)]
    if len(assembly_indices) != 1:
        raise AnimationError("PACK_AND_GO_PREFLIGHT_FAILED", "Source assembly is not uniquely included in the package.")
    if not _pack_call("SetSaveToName", package.SetSaveToName, True, str(target)):
        raise AnimationError("PACK_AND_GO_PREFLIGHT_FAILED", "SetSaveToName returned false.")
    if os.path.normcase(os.path.abspath(_pack_call("GetSaveToName", package.GetSaveToName))) != os.path.normcase(str(target)):
        raise AnimationError("PACK_AND_GO_PREFLIGHT_FAILED", "Native destination directory readback differs.")
    _, raw_destinations, document_statuses = _pack_outputs(
        "GetDocumentSaveToNames", package.GetDocumentSaveToNames, 2, vtable)
    if (not isinstance(raw_destinations, (tuple, list)) or len(raw_destinations) != len(sources)
            or not isinstance(document_statuses, (tuple, list)) or len(document_statuses) != len(sources)):
        raise AnimationError("PACK_AND_GO_PREFLIGHT_FAILED", "Native destination/status counts differ from source count.")
    for index, raw in enumerate(raw_destinations):
        if not isinstance(raw, str) or not Path(raw).is_absolute():
            raise AnimationError("PACK_AND_GO_PREFLIGHT_FAILED", f"Invalid native destination: {raw!r}.")
        actual = Path(raw)
        if (not _inside_file(actual, target)
                or os.path.normcase(str(actual.resolve())) != os.path.normcase(str(destinations[index]))):
            raise AnimationError("PACK_AND_GO_PREFLIGHT_FAILED", f"Native destination did not match isolated mapping: {raw!r}.")
        if document_statuses[index] != 0:
            raise AnimationError("PACK_AND_GO_PREFLIGHT_FAILED", f"Only normal local files supported; source status={document_statuses[index]!r}.")
    mapping = [{"source": str(source), "destination": str(dest)} for source, dest in zip(sources, destinations)]
    assembly_path = destinations[assembly_indices[0]]
    result = {"ok": False, "source_document_path": str(source_path), "output_directory": str(target),
              "assembly_path": str(assembly_path), "mapping": mapping, "source_saved": False,
              "opened": False, "prefix": prefix, "file_count": len(mapping)}
    stage = "claim_output_directory"
    write_attempted = False
    try:
        target.mkdir(exist_ok=False)
        stage = "native_save_pack_and_go"
        if any(target.iterdir()):
            raise RuntimeError("New output directory is no longer empty; refusing to overwrite files.")
        write_attempted = True
        statuses = extension.SavePackAndGo(package)
        result["native_statuses"] = list(statuses) if isinstance(statuses, (tuple, list)) else repr(statuses)
        if (not isinstance(statuses, (tuple, list)) or len(statuses) != len(sources)
                or any(type(code) is not int or code != 0 for code in statuses)):
            raise RuntimeError("Native Pack and Go did not return one success status0 per source.")
        stage = "saved_file_verification"
        for source, destination in zip(sources, destinations):
            if (not _inside_file(destination, target) or destination.is_symlink()
                    or not destination.is_file() or destination.stat().st_size == 0 or _same_file(source, destination)):
                raise RuntimeError(f"Expected independent nonempty cloned file is missing or invalid: {destination}.")
            if (source.stat().st_size, source.stat().st_mtime_ns) != source_metadata[str(source)]:
                raise RuntimeError(f"Source file metadata changed during Pack and Go: {source}.")
        stage = "saved_reference_isolation"
        dependencies = sw_app.GetDocumentDependencies2(str(assembly_path), True, False, False)
        if not isinstance(dependencies, (tuple, list)) or len(dependencies) % 2:
            raise RuntimeError(f"Unexpected saved dependency array: {dependencies!r}.")
        reference_paths = [str(dependencies[index]) for index in range(1, len(dependencies), 2)]
        result["saved_reference_paths"] = reference_paths
        if len(sources) > 1 and not reference_paths:
            raise RuntimeError("The saved assembly returned no references despite containing multiple source documents.")
        for dependency in reference_paths:
            dep = Path(dependency)
            if not dep.is_absolute() or not _inside_file(dep, target) or not dep.is_file():
                raise RuntimeError(f"Cloned assembly retains an external or missing reference: {dependency!r}.")
        if not _same_file(str(model.GetPathName()), source_path):
            raise RuntimeError("The active source document identity changed unexpectedly.")
        result.update(ok=True, verified=True, reference_isolation_verified=True, source_metadata_unchanged=True)
    except Exception as exc:
        result.update(code="PACK_AND_GO_FAILED", failed={"stage": stage, "error": str(exc)},
                      partial_files_possible=write_attempted)
    result["write_attempted"] = write_attempted
    return result


pack_and_go = clone
