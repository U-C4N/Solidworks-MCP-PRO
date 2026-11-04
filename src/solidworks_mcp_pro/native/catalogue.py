"""Functional public signatures and independently implemented tool dispatch."""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from importlib import resources
import json

from mcp import types

from .errors import CadError


READ_ONLY = frozenset({"get_status", "list_documents", "list_bodies", "list_dimensions", "read_sketch",
    "list_features", "list_equations", "get_mass_properties", "get_bounding_box", "list_faces", "list_edges",
    "list_components", "get_assembly_bounding_box", "measure_distance", "check_printability", "slice_mesh",
    "compare_with_mesh", "check_interference"})
OFFLINE = frozenset({"slice_mesh"})
DOCUMENT_OPERATIONS = frozenset({"get_status", "new_part", "list_documents", "activate_document", "close_part",
    "save_part", "open_part", "new_assembly", "open_assembly", "save_assembly", "export", "screenshot", "make_drawing"})


@lru_cache(maxsize=1)
def contracts() -> dict:
    return json.loads(resources.files(__package__).joinpath("contracts.json").read_text(encoding="utf-8"))


def handler(name: str):
    if name not in contracts():
        raise CadError("UNKNOWN_TOOL", f"Unknown native tool {name!r}.")
    from . import geometry, features, assembly, topology, mesh, documents
    for group in (documents, geometry, features, assembly, topology, mesh):
        if name in group.TOOLS:
            return group.TOOLS[name]
    raise CadError("ENGINE_CONTRACT_INCOMPLETE", f"Native implementation missing for {name!r}.")


def list_tools() -> list[types.Tool]:
    descriptions = {
        "get_status": "Inspect the running CAD connection and exact current target without launching SOLIDWORKS.",
        "new_part": "Create a new native part from the installed default template and make it the session target.",
        "new_assembly": "Create a new native assembly from the installed default template.",
        "activate_document": "Explicitly target an open document. Existing documents require allow_edit=true before modeling.",
        "open_part": "Open a native part without overwriting it. allow_edit explicitly authorizes modifications.",
        "open_assembly": "Open a native assembly without overwriting it. allow_edit explicitly authorizes modifications.",
    }
    return [types.Tool(name=name, description=descriptions.get(name,
        f"{name.replace('_', ' ').capitalize()} through the independent native SOLIDWORKS engine. "
        "Public geometry units are mm, degrees and kg. Inspect verified measurements and error evidence."),
        inputSchema=deepcopy(schema)) for name, schema in contracts().items()]


def validate_coverage():
    implementations = {name: handler(name) for name in contracts()}
    if len(implementations) != 88:
        raise CadError("ENGINE_CONTRACT_INCOMPLETE", "The independent compatibility catalogue must contain 88 implementations.")
    return implementations

