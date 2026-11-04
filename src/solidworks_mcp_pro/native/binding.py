"""Lazy Python bindings generated from the user's installed SOLIDWORKS SDK.

Only native type libraries are loaded. No other CAD MCP implementation is used.
COM objects must be accessed on the STA that owns them; module metadata may be
cached process-wide. Fake objects are deliberately left untouched in unit tests.
"""
from __future__ import annotations

import os
from pathlib import Path
import threading
from typing import Any

from .errors import CadError


_LIBRARIES = {"sldworks": "{83A33D31-27C5-11CE-BFD4-00400513BB57}",
              "swconst": "{4687F359-55D0-4CD3-B6CF-2EB42C11F989}"}
_modules: dict[str, Any] = {}
_lock = threading.RLock()


def typelib_path(name: str = "sldworks") -> Path:
    if name not in _LIBRARIES:
        raise CadError("TYPE_LIBRARY_UNKNOWN", "Only installed SOLIDWORKS and constant libraries are supported.")
    override = os.environ.get("SW_MCP_TYPELIB")
    if override:
        path = Path(override) if name == "sldworks" else Path(override).with_name("swconst.tlb")
        if path.is_file():
            return path.resolve()
        raise CadError("TYPE_LIBRARY_NOT_FOUND", "The configured native type library does not exist.")
    candidates = [Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) /
                  "SOLIDWORKS Corp" / "SOLIDWORKS" / f"{name}.tlb"]
    if os.name == "nt":
        import winreg
        registered = []
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "TypeLib\\" + _LIBRARIES[name]) as library:
                versions = []
                for index in range(winreg.QueryInfoKey(library)[0]):
                    version = winreg.EnumKey(library, index)
                    try:
                        order = tuple(int(part, 16) for part in version.split("."))
                        versions.append((order, version))
                    except ValueError:
                        continue
                for _, version in sorted(versions, reverse=True):
                    for platform in ("win64", "win32"):
                        try:
                            with winreg.OpenKey(library, version + "\\0\\" + platform) as key:
                                registered.append(Path(winreg.QueryValueEx(key, None)[0]))
                        except OSError:
                            continue
        except OSError:
            pass
        candidates = registered + candidates
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise CadError("TYPE_LIBRARY_NOT_FOUND", "Install SOLIDWORKS or set SW_MCP_TYPELIB to its native sldworks.tlb.")


def module(name: str = "sldworks"):
    with _lock:
        if name not in _modules:
            if os.name != "nt":
                raise CadError("NATIVE_UNAVAILABLE", "Native SOLIDWORKS COM is available only on Windows.")
            import pythoncom
            from win32com.client import gencache
            library = pythoncom.LoadTypeLib(str(typelib_path(name)))
            guid, lcid, _, major, minor, _ = library.GetLibAttr()
            generated = gencache.EnsureModule(guid, lcid, major, minor)
            if generated is None:
                raise CadError("TYPE_LIBRARY_GENERATION_FAILED", "Cannot generate bindings for the installed native SDK.")
            _modules[name] = generated
        return _modules[name]


def constant(name: str) -> int:
    constants = module("swconst").constants
    try:
        return int(getattr(constants, name))
    except AttributeError as exc:
        raise CadError("NATIVE_CONSTANT_UNAVAILABLE", f"Installed SDK has no constant {name!r}.") from exc


def wrap(value, interface):
    if value is None or not hasattr(value, "_oleobj_"):
        return value
    import pythoncom
    cls = getattr(module(), interface) if isinstance(interface, str) else interface
    dispatch = value._oleobj_.QueryInterface(cls.CLSID, pythoncom.IID_IDispatch)
    return cls(dispatch)


def array_double(values):
    values = [float(value) for value in values]
    if os.name != "nt":
        return values
    import pythoncom
    from win32com.client import VARIANT
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, values)


def array_dispatch(values, *, force=False):
    values = list(values)
    if os.name != "nt" or (not force and not any(hasattr(value, "_oleobj_") for value in values)):
        return values
    import pythoncom
    from win32com.client import VARIANT
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, values)


def array_string(values):
    values = [str(value) for value in values]
    if os.name != "nt":
        return values
    import pythoncom
    from win32com.client import VARIANT
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_BSTR, values)


def null_dispatch():
    if os.name != "nt":
        return None
    import pythoncom
    from win32com.client import VARIANT
    return VARIANT(pythoncom.VT_DISPATCH, None)

