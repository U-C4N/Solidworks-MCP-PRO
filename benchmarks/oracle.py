"""Read-only geometry oracle, separate from the benchmarked MCP responses.

Uses SOLIDWORKS mass properties with forced SI units, then converts to mm.
Never creates, saves, activates, closes or modifies document geometry.
"""
from __future__ import annotations

from contextlib import contextmanager


@contextmanager
def connection():
    import pythoncom
    import win32com.client
    from solidworks_mcp import binding

    pythoncom.CoInitialize()
    try:
        raw = win32com.client.GetActiveObject("SldWorks.Application")
        revision = win32com.client.dynamic.DumbDispatch(raw._oleobj_).RevisionNumber
        module = binding._load_module(revision)
        app = binding.wrap(raw, module.ISldWorks)
        yield app, module, binding
    finally:
        pythoncom.CoUninitialize()


def snapshot(expected_title: str | None = None) -> dict:
    with connection() as (app, module, binding):
        documents = []
        for raw in app.GetDocuments() or []:
            doc = binding.wrap(raw, module.IModelDoc2)
            documents.append({"title": doc.GetTitle(), "path": doc.GetPathName(), "dirty": bool(doc.GetSaveFlag())})
        doc = binding.wrap(app.ActiveDoc, module.IModelDoc2)
        revision = app.RevisionNumber
        result = {"revision": str(revision() if callable(revision) else revision), "documents": documents,
                  "active_title": doc.GetTitle() if doc is not None else None}
        if doc is None:
            return result
        if expected_title is not None and doc.GetTitle() != expected_title:
            raise ValueError(f"Oracle target mismatch: expected {expected_title!r}, active {doc.GetTitle()!r}")
        if int(doc.GetType()) != 1:
            return {**result, "type": int(doc.GetType())}
        ext = binding.wrap(doc.Extension, module.IModelDocExtension)
        mass = binding.wrap(ext.CreateMassProperty(), module.IMassProperty)
        if mass is None:
            return result
        mass.UseSystemUnits = True
        if not mass.UseSystemUnits:
            raise RuntimeError("Oracle could not force SI units")
        part = binding.wrap(doc, module.IPartDoc)
        bounds = part.GetPartBox(True)
        result.update(volume_mm3=float(mass.Volume) * 1e9,
                      surface_area_mm2=float(mass.SurfaceArea) * 1e6,
                      bounds_mm=[float(value) * 1000 for value in bounds] if bounds else None)
        if bounds:
            result["size_mm"] = [(float(bounds[i + 3]) - float(bounds[i])) * 1000 for i in range(3)]
        return result
