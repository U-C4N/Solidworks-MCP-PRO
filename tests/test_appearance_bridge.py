from pathlib import Path
from types import SimpleNamespace

import pytest

from solidworks_mcp_pro import appearance_bridge as bridge


class Material:
    def __init__(self, path, ids):
        self.FileName = path
        self.PrimaryColor = 42
        self.ids = ids
        self.entities = []
        self.reject_entity = False

    def AddEntity(self, entity):
        if self.reject_entity:
            return False
        self.entities.append(entity)
        return True

    def GetMaterialIds(self):
        return self.ids

    def GetEntities(self):
        return self.entities

    def GetEntitiesCount(self):
        return len(self.entities)


class Extension:
    def __init__(self):
        self.created = []
        self.materials = []
        self.fail_at = None
        self.hide_readback = False
        self.reject_entity = False

    def CreateRenderMaterial(self, path):
        material = Material(path, (len(self.created) + 1, 0))
        material.reject_entity = self.reject_entity
        self.created.append(material)
        return material

    def AddDisplayStateSpecificRenderMaterial(self, material, scope, names):
        assert (scope, names) == (1, None)
        if len(self.created) == self.fail_at:
            raise RuntimeError("native rejected second appearance")
        self.materials.append(material)
        return True, *material.ids

    def GetRenderMaterials2(self, scope, names):
        assert (scope, names) == (1, None)
        return [] if self.hide_readback else self.materials

    def GetRenderMaterialsCount2(self, scope, names):
        assert (scope, names) == (1, None)
        return len(self.materials)


class Model:
    def __init__(self, path):
        self.path = path
        self.Extension = Extension()
        self.components = [SimpleNamespace(Name2="Frame-1"), SimpleNamespace(Name2="Handle-1")]
        self.read_only = False
        self.view_only = False
        self.doc_type = 2

    def GetPathName(self):
        return str(self.path)

    def GetType(self):
        return self.doc_type

    def IsOpenedReadOnly(self):
        return self.read_only

    def IsOpenedViewOnly(self):
        return self.view_only

    def GetComponents(self, top_only):
        assert top_only is False
        return self.components


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "owned-render.sldasm"
    material = tmp_path / "blue.p2m"
    path.write_bytes(b"fixture assembly")
    material.write_bytes(b"fixture material")
    model = Model(path)
    app = SimpleNamespace(ActiveDoc=model)
    assignments = [{"component": "Frame-1", "material_path": str(material), "color_rgb": [20, 80, 160]}]
    return app, path, assignments


def test_applies_assembly_only_and_reads_back_ids_entities_color(setup):
    app, path, assignments = setup
    result = bridge.apply(app, path, assignments)
    assert result["ok"]
    assert result["saved"] is False
    assert result["rollback_performed"] is False
    record = result["applied"][0]
    assert record["accepted"] and record["verified"]
    assert record["material_ids"] == [1, 0]
    assert record["readback"]["components"] == ["Frame-1"]
    assert record["readback"]["primary_color"] == 20 | (80 << 8) | (160 << 16)
    assert app.ActiveDoc.Extension.created[0].entities == [app.ActiveDoc.components[0]]


@pytest.mark.parametrize("change", [
    {"component": "missing"}, {"component": None}, {"component": ""},
    {"material_path": None}, {"material_path": "relative.p2m"},
    {"color_rgb": None}, {"color_rgb": [True, 0, 0]}, {"color_rgb": [0.1, 0, 0]},
    {"color_rgb": [256, 0, 0]}, {"color_rgb": [-1, 0, 0]}, {"color_rgb": [1, 2]},
    {"unexpected": "no"},
])
def test_entire_batch_validated_before_native_mutation(setup, change):
    app, path, assignments = setup
    invalid = {**assignments[0], "component": "Handle-1", **change}
    with pytest.raises(bridge.AppearanceError):
        bridge.apply(app, path, [assignments[0], invalid])
    assert app.ActiveDoc.Extension.created == []


def test_duplicate_assignments_and_ambiguous_native_names_refused(setup):
    app, path, assignments = setup
    with pytest.raises(bridge.AppearanceError, match="unique"):
        bridge.apply(app, path, assignments * 2)
    app.ActiveDoc.components.append(SimpleNamespace(Name2="Frame-1"))
    with pytest.raises(bridge.AppearanceError, match="COMPONENT_NOT_UNIQUE"):
        bridge.apply(app, path, assignments)
    assert not app.ActiveDoc.Extension.created


@pytest.mark.parametrize("flag", ["read_only", "view_only"])
def test_read_only_or_view_only_document_refused(setup, flag):
    app, path, assignments = setup
    setattr(app.ActiveDoc, flag, True)
    with pytest.raises(bridge.AppearanceError, match="DOCUMENT_READ_ONLY"):
        bridge.apply(app, path, assignments)
    assert not app.ActiveDoc.Extension.created


def test_exact_saved_active_assembly_required(setup, tmp_path):
    app, path, assignments = setup
    app.ActiveDoc.path = tmp_path / "another.sldasm"
    with pytest.raises(bridge.AppearanceError, match="DOCUMENT_MISMATCH"):
        bridge.apply(app, path, assignments)
    app.ActiveDoc.path = ""
    with pytest.raises(bridge.AppearanceError, match="DOCUMENT_MISMATCH"):
        bridge.apply(app, path, assignments)
    app.ActiveDoc.path = path
    app.ActiveDoc.doc_type = 1
    with pytest.raises(bridge.AppearanceError, match="ASSEMBLY_REQUIRED"):
        bridge.apply(app, path, assignments)
    app.ActiveDoc = None
    with pytest.raises(bridge.AppearanceError, match="NO_ACTIVE_DOCUMENT"):
        bridge.apply(app, path, assignments)


@pytest.mark.parametrize("scene", ["scene.p2s", "", Path("scene.p2s")])
def test_scene_rejected_before_mutation(setup, scene):
    app, path, assignments = setup
    with pytest.raises(bridge.AppearanceError, match="SCENE_UNSUPPORTED"):
        bridge.apply(app, path, assignments, scene_path=scene)
    assert not app.ActiveDoc.Extension.created


def test_partial_failure_reports_first_accepted_ids_without_false_rollback(setup):
    app, path, assignments = setup
    app.ActiveDoc.Extension.fail_at = 2
    assignments.append({**assignments[0], "component": "Handle-1"})
    result = bridge.apply(app, path, assignments)
    assert result["ok"] is False
    assert result["partial_state_possible"]
    assert result["rollback_performed"] is False
    assert result["applied"][0]["verified"]
    assert result["applied"][0]["material_ids"] == [1, 0]
    assert result["failed"]["component"] == "Handle-1"
    assert result["failed"]["stage"] == "apply_current_display_state"
    assert len(app.ActiveDoc.Extension.materials) == 1


def test_native_acceptance_without_readback_never_reports_success(setup):
    app, path, assignments = setup
    app.ActiveDoc.Extension.hide_readback = True
    result = bridge.apply(app, path, assignments)
    assert result["ok"] is False
    assert result["applied"][0]["accepted"]
    assert result["applied"][0]["verified"] is False
    assert result["failed"]["stage"] == "readback"


def test_rejected_component_never_applies_to_part_as_fallback(setup):
    app, path, assignments = setup
    app.ActiveDoc.Extension.reject_entity = True
    result = bridge.apply(app, path, assignments)
    assert result["ok"] is False
    assert result["failed"]["stage"] == "add_component_entity"
    assert not app.ActiveDoc.Extension.materials


def test_without_rgb_preserves_native_material_color(setup):
    app, path, assignments = setup
    assignments[0].pop("color_rgb")
    result = bridge.apply(app, path, assignments)
    assert result["ok"]
    assert result["applied"][0]["readback"]["primary_color"] == 42


def test_callable_com_property_values_never_invoke_default_member(setup):
    app, path, assignments = setup

    class CallableModel(Model):
        def __call__(self):
            pytest.fail("ActiveDoc is a property; do not invoke its returned dispatch")

    class CallableExtension(Extension):
        def __call__(self):
            pytest.fail("Extension is a property; do not invoke its returned dispatch")

    class CallableString(str):
        def __call__(self):
            pytest.fail("Name2 is a property, not a method")

    app.ActiveDoc = CallableModel(path)
    app.ActiveDoc.Extension = CallableExtension()
    app.ActiveDoc.components[0].Name2 = CallableString("Frame-1")
    assert bridge.apply(app, path, assignments)["ok"]


def test_component_collection_alone_does_not_prove_assembly_override(setup):
    app, path, assignments = setup
    extension = app.ActiveDoc.Extension
    extension.hide_readback = True
    app.ActiveDoc.components[0].GetRenderMaterials2 = lambda scope, names: extension.materials
    result = bridge.apply(app, path, assignments)
    assert result["ok"] is False
    assert result["failed"]["stage"] == "readback"


def test_readback_failure_reports_observed_collection_ids(setup):
    app, path, assignments = setup
    extension = app.ActiveDoc.Extension
    extension.hide_readback = True
    unrelated = Material("other.p2m", (72, 9))
    app.ActiveDoc.components[0].GetRenderMaterials2 = lambda scope, names: [unrelated]
    result = bridge.apply(app, path, assignments)
    assert result["ok"] is False
    assert "72, 9" in result["failed"]["error"]
    assert "assembly_extension" in result["failed"]["error"]
    assert "component" in result["failed"]["error"]


def test_dry_run_reads_existing_state_without_creating_materials(setup):
    app, path, assignments = setup
    original = Material(assignments[0]["material_path"], (5, 0))
    original.entities = [app.ActiveDoc.components[0]]
    app.ActiveDoc.components[0].GetRenderMaterials2 = lambda scope, names: [original]
    result = bridge.apply(app, path, assignments, dry_run=True)
    assert result["ok"] and result["dry_run"]
    assert result["mutation_attempted"] is False
    assert not app.ActiveDoc.Extension.created
    actual = result["components"][0]["collection"]["materials"][0]
    assert actual["material_ids"] == [5, 0]
    assert actual["primary_color"] == 42
    assert actual["entities"] == [{"component": "Frame-1"}]


def test_native_return_and_direct_material_ids_are_preserved(setup):
    app, path, assignments = setup
    result = bridge.apply(app, path, assignments)
    record = result["applied"][0]
    assert record["native_result"] == [True, 1, 0]
    assert record["material_ids_before_apply"] == [1, 0]
    assert record["material_ids_after_apply"] == [1, 0]


def test_remapped_ids_require_unique_actual_file_entity_color_state(setup):
    app, path, assignments = setup
    extension = app.ActiveDoc.Extension

    def native_apply(material, scope, names):
        fresh = Material(material.FileName, (4, 0))
        fresh.PrimaryColor = material.PrimaryColor
        fresh.entities = material.entities[:]
        extension.materials.append(fresh)
        return True, *material.ids

    extension.AddDisplayStateSpecificRenderMaterial = native_apply
    result = bridge.apply(app, path, assignments)
    assert result["ok"]
    record = result["applied"][0]
    assert record["material_ids"] == [1, 0]
    assert record["readback"]["material_ids"] == [4, 0]
    assert record["readback"]["id_scope_mismatch"] is True
    assert record["readback"]["source"] == "assembly_extension"


def test_matching_path_and_entity_never_hides_reset_requested_color(setup):
    app, path, assignments = setup
    extension = app.ActiveDoc.Extension

    def native_apply(material, scope, names):
        fresh = Material(material.FileName, (4, 0))
        fresh.entities = material.entities[:]
        extension.materials.append(fresh)
        return True, *material.ids

    extension.AddDisplayStateSpecificRenderMaterial = native_apply
    result = bridge.apply(app, path, assignments)
    assert result["ok"] is False
    assert "Color readback differs" in result["failed"]["error"]
