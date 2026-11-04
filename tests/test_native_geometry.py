"""Independent geometry math and native-boundary tests; no CAD process required."""
from types import SimpleNamespace
import math
import sys

import pytest

from solidworks_mcp_pro.native import geometry as g
from solidworks_mcp_pro.native.errors import CadError


class Entity:
    def __init__(self, model):
        self.model = model

    def Select2(self, append, mark):
        if not append:
            self.model.selected.clear()
        self.model.selected.append((self, mark))
        return True

    def Select4(self, append, data):
        return self.Select2(append, getattr(data, "Mark", 0))


class Feature(Entity):
    def __init__(self, model, kind="ProfileFeature", name=None):
        super().__init__(model)
        self.Name = name or f"{kind}{len(model.features)+1}"
        self.kind = kind
        self.error = (0, False)
        self.definition = None
        self.offset = 0.
        model.features.append(self)

    def GetTypeName2(self):
        return self.kind

    def GetSpecificFeature2(self):
        return getattr(self, 'specific', None)

    def GetNextFeature(self):
        index = self.model.features.index(self)+1
        return self.model.features[index] if index < len(self.model.features) else None

    def GetFirstSubFeature(self):
        return None

    def GetErrorCode2(self):
        return self.error

    def GetDefinition(self):
        return self.definition

    def ModifyDefinition(self, definition, model, component):
        self.definition = definition
        return True


class Point(Entity):
    def __init__(self, model, xyz, sketch=None):
        super().__init__(model)
        self.X, self.Y, self.Z = xyz
        self.sketch = sketch

    def GetSketch(self):
        return self.sketch

    def GetPoint(self):
        return [self.X, self.Y, self.Z]

    def SetCoords(self, x, y, z):
        assert self.model.active is self.sketch, 'Native position setters require editing their sketch.'
        self.X, self.Y, self.Z = x, y, z
        return True


class Segment(Entity):
    def __init__(self, model, start, end, *, kind="line", center=None):
        super().__init__(model)
        self.start = Point(model, start, model.active)
        self.end = Point(model, end, model.active)
        self.center = Point(model, center or start, model.active)
        self.kind = kind
        self.ConstructionGeometry = False
        self.index = len(model.active.segments)
        model.active.segments.append(self)

    def GetStartPoint2(self):
        return self.start

    def GetEndPoint2(self):
        return self.end

    def GetCenterPoint2(self):
        return self.center

    def GetID(self):
        return (0, self.index)


class Sketch:
    def __init__(self, model, support=None):
        self.model = model
        self.feature = Feature(model)
        self.feature.specific = self
        self.segments = []
        self.ModelToSketchTransform = SimpleNamespace(offset=getattr(support, "offset", 0.))

    def GetSketchSegments(self):
        return self.segments

    def Is3D(self):
        return False


class Dimension:
    def __init__(self, value=0., name="D1", owner="Sketch1"):
        self.SystemValue, self.Name, self.owner = value, name, owner

    def GetNameForSelection(self):
        return self.Name+"@"+self.owner


class Manager:
    def __init__(self, model):
        self.model, self.AddToDB = model, False

    def InsertSketch(self, update):
        if self.model.active is None:
            support = self.model.selected[-1][0]
            self.model.active = Sketch(self.model, support)
        else:
            self.model.active = None

    def Insert3DSketch(self, update):
        self.model.active = None if self.model.active is not None else Sketch(self.model)

    def CreateLine(self, *values):
        return Segment(self.model, values[:3], values[3:])

    def Create3PointArc(self, *values):
        self.model.calls.append(("Create3PointArc", values))
        return Segment(self.model, values[:3], values[3:6], kind="arc")

    def CreateCenterLine(self, *values):
        line = self.CreateLine(*values)
        line.ConstructionGeometry = True
        return line

    def CreateCornerRectangle(self, x1, y1, z1, x2, y2, z2):
        corners = [[x1, y1, z1], [x2, y1, z1], [x2, y2, z2], [x1, y2, z2]]
        return [Segment(self.model, a, b) for a, b in zip(corners, corners[1:]+corners[:1])]

    def CreateCircleByRadius(self, x, y, z, radius):
        self.model.calls.append(("CreateCircleByRadius", (x, y, z, radius)))
        return Segment(self.model, [x+radius, y, z], [x+radius, y, z], kind="circle", center=[x, y, z])

    def CreateSpline2(self, values, smooth):
        self.model.calls.append(("CreateSpline2", (values, smooth)))
        return Segment(self.model, values[:3], values[-3:], kind="spline")

    def SketchUseEdge3(self, chain, inner):
        selected = self.model.selected[-1][0]
        edges = selected.GetEdges() if isinstance(selected, Face) else [selected]
        for edge in edges:
            self.CreateLine(*edge.GetStartVertex().GetPoint(), *edge.GetEndVertex().GetPoint())
        return True

    def SketchOffset2(self, *args):
        self.model.calls.append(("SketchOffset2", args))
        return True


class FeatureManager:
    def __init__(self, model):
        self.model = model
        self.reject = set()

    def __getattr__(self, operation):
        if operation not in {'FeatureExtrusion3', 'FeatureCut4', 'FeatureRevolve2', 'InsertRefPlane',
                             'InsertRib', 'HoleWizard5', 'InsertProtrusionBlend2', 'InsertProtrusionSwept4'}:
            raise AttributeError(operation)
        def invoke(*args):
            self.model.calls.append((operation, args))
            if operation in self.reject:
                return None
            kind = {'InsertRefPlane': 'RefPlane', 'InsertRib': 'Rib'}.get(operation, operation)
            feature = Feature(self.model, kind)
            if operation == "FeatureExtrusion3":
                self.model.depth = args[5]
            if operation == "InsertRefPlane":
                feature.offset = args[1] * (-1 if args[0] & 256 else 1)
                feature.specific = SimpleNamespace(native_reference_plane=True)
                return feature.specific
            if operation == 'InsertRib':
                return None
            if operation == "HoleWizard5":
                sketch = Sketch(self.model, self.model.face)
                position = Point(self.model, [0., 0., 0.], sketch)
                feature.definition = SimpleNamespace(
                    AccessSelections=lambda *v: True, ReleaseSelectionAccess=lambda: None,
                    GetSketchPoints=lambda: [position], FastenerSize=args[3], Diameter=.006,
                    Depth=.01, ThreadDepth=.01)
            return feature
        return invoke


class Edge(Entity):
    def __init__(self, model, a, b):
        super().__init__(model)
        self.a, self.b = Point(model, a), Point(model, b)

    def GetCurve(self):
        return SimpleNamespace(IsLine=lambda: True)

    def GetStartVertex(self):
        return self.a

    def GetEndVertex(self):
        return self.b


class Face(Entity):
    offset = .01
    Normal = [0., 0., 1.]

    def GetSurface(self):
        return SimpleNamespace(IsPlane=lambda: True)

    def GetClosestPointOn(self, x, y, z):
        return [x, y, self.offset, 0., 0.]

    def GetEdges(self):
        points = [[0., 0., .01], [.1, 0., .01], [.1, .07, .01], [0., .07, .01]]
        return [Edge(self.model, a, b) for a, b in zip(points, points[1:]+points[:1])]


class Model:
    def __init__(self):
        self.features, self.calls, self.selected = [], [], []
        self.active = None
        self.depth = .01
        self.SketchManager = Manager(self)
        self.FeatureManager = FeatureManager(self)
        self.SelectionManager = SimpleNamespace(CreateSelectData=lambda: SimpleNamespace(Mark=0))
        self.Extension = SimpleNamespace(SelectByID2=lambda *args: True)
        self.face = Face(self)
        self.plane = Feature(self, "RefPlane", "Front")

    def GetActiveSketch2(self):
        return self.active

    def FirstFeature(self):
        return self.features[0] if self.features else None

    def GetBodies2(self, kind, visible_only):
        def select(append, data):
            self.calls.append(('SelectRibBody', (append, data.Mark)))
            return True
        return [SimpleNamespace(Select2=select)]

    def ClearSelection2(self, value):
        self.selected.clear()

    def AddDimension2(self, *args):
        self.calls.append(("AddDimension2", args))
        dimension = Dimension(owner=self.active.feature.Name)
        return SimpleNamespace(GetDimension2=lambda index: dimension)

    def Parameter(self, name):
        return Dimension(self.depth, name.split("@")[0], name.split("@")[1])

    def SketchAddConstraints(self, relation):
        self.calls.append(("SketchAddConstraints", relation))

    def EditRebuild3(self):
        return True

    def ForceRebuild3(self, top_only):
        self.calls.append(('ForceRebuild3', (top_only,)))
        return True

    def EditSketch(self):
        self.active = self.selected[-1][0].GetSpecificFeature2()

    def InsertSketchText(self, *args):
        formatting = SimpleNamespace(CharHeight=.001, TypeFaceName="Arial")
        return SimpleNamespace(GetTextFormat=lambda: formatting, SetTextFormat=lambda *values: True)


class MathPoint:
    def __init__(self, values):
        self.ArrayData = list(values)

    def MultiplyTransform(self, transform):
        x, y, z = self.ArrayData
        return MathPoint([x, y, z-transform.offset])


class Session:
    def __init__(self):
        self.model = Model()
        self.prompt = True
        self.preferences = []

    def _require_part(self):
        return self.model

    _require_model = _require_part

    def _ensure(self):
        return self

    def GetUserPreferenceToggle(self, index):
        return self.prompt

    def SetUserPreferenceToggle(self, index, value):
        self.preferences.append((index, value))
        self.prompt = value

    def GetMathUtility(self):
        return SimpleNamespace(CreatePoint=lambda values: MathPoint(values))

    def resolve_plane(self, name):
        if name in {"front", "top", "right"}:
            return self.model.plane
        return self.find_feature(name)

    def find_feature(self, name):
        return next((feature for feature in self.model.features if feature.Name == name), None)

    def resolve_face(self, ref, point_mm=None):
        return self.model.face

    def resolve_edges(self, edges):
        return self.model.face.GetEdges()[:len(edges)]

    def select(self, entity, mark=0, append=False):
        return entity.Select2(append, mark)

    def feature_result(self, feature, name=None, **extra):
        if name:
            feature.Name = name
        return {"ok": True, "feature": feature.Name, **extra}


@pytest.fixture
def session(monkeypatch):
    original_wrap = g.binding.wrap
    def wrap(value, interface):
        if isinstance(value, Sketch) and interface == 'IFeature':
            raise AssertionError('A sketch default dispatch cannot be selected as IFeature.')
        return original_wrap(value, interface)
    monkeypatch.setattr(g.binding, 'wrap', wrap)
    monkeypatch.setattr(g.binding, "array_double", lambda values: list(values))
    monkeypatch.setattr(g.binding, "null_dispatch", lambda: None)
    return Session()


def test_box_native_si_parameters_and_real_dimension_names(session):
    result = g.add_box(session, 100, 70, 10)
    args = next(args for operation, args in session.model.calls if operation == "FeatureExtrusion3")
    assert len(args) == 23 and args[5] == .01 and args[17] is True
    assert result["depth_dimension"] == "D1@BlockExtrude"
    assert result["dimensions"]["width"].startswith("width@")
    assert result["dimensions"]["height"].startswith("height@")
    assert session.model.GetActiveSketch2() is None
    assert session.prompt and session.preferences == [(10, False), (10, True)]*2


def test_sketch_owner_matches_tree_specific_feature_instead_of_latest_name(session):
    active = Sketch(session.model)
    Sketch(session.model)
    session.model.active = active
    assert g._sketch_owner(session.model, active) is active.feature
    assert g.finish_sketch(session) is active.feature
    assert session.model.active is None


def test_new_sketch_owner_can_use_unique_tree_difference(session):
    old = Sketch(session.model)
    before = {old.feature.Name}
    active = Sketch(session.model)
    active.feature.specific = None
    assert g._sketch_owner(session.model, active, before=before) is active.feature
    with pytest.raises(CadError, match='unique'):
        g._sketch_owner(session.model, active)
    another = Sketch(session.model)
    another.feature.specific = None
    with pytest.raises(CadError, match='unique'):
        g._sketch_owner(session.model, active, before=before)


def test_sketch_matching_uses_canonical_iunknown_identity(monkeypatch):
    monkeypatch.setitem(sys.modules, 'pythoncom', SimpleNamespace(IID_IUnknown='IUnknown'))
    shared_identity = object()
    distinct_identity = object()
    def dispatch(identity):
        return SimpleNamespace(_oleobj_=SimpleNamespace(QueryInterface=lambda iid: identity))
    assert g._same_native_identity(dispatch(shared_identity), dispatch(shared_identity))
    assert not g._same_native_identity(dispatch(shared_identity), dispatch(distinct_identity))


@pytest.mark.parametrize("width,height,depth", [(0, 10, 10), (10, -1, 10), (10, 10, float('nan')), (True, 10, 10)])
def test_invalid_box_has_no_native_edits(session, width, height, depth):
    with pytest.raises(CadError):
        g.add_box(session, width, height, depth)
    assert not session.model.calls and len(session.model.features) == 1


def test_rejected_feature_is_never_reported_success(session):
    session.model.FeatureManager.reject.add("FeatureExtrusion3")
    with pytest.raises(CadError, match="no native feature"):
        g.add_disc(session, 20, 5)
    assert session.model.active is None and session.prompt


def test_symmetric_cut_uses_two_native_sides_and_half_depth(session):
    g.cut_profile_through_plane(session, [[0, 0], [30, 0], [30, 20]], "front", depth_mm=12)
    args = next(args for op, args in session.model.calls if op == "FeatureCut4")
    assert len(args) == 27 and args[0] is False and args[3:7] == (0, 0, .006, .006)


def test_keep_inside_uses_native_reverse_contour_and_both_through_all_sides(session):
    g.cut_profile_through_plane(session, [[0, 0], [30, 0], [30, 20]], "front", keep_inside=True)
    args = next(args for op, args in session.model.calls if op == "FeatureCut4")
    assert args[:7] == (False, True, False, 1, 1, 0., 0.)


def test_invalid_keep_inside_depth_fails_before_sketch(session):
    with pytest.raises(CadError):
        g.cut_profile_through_plane(session, [[0, 0], [30, 0], [30, 20]], "front", depth_mm=2, keep_inside=True)
    assert not session.model.calls


def test_disc_and_cylinder_have_distinct_native_construction_axes(session):
    g.add_disc(session, 20, 40)
    assert any(op == "FeatureExtrusion3" for op, _ in session.model.calls)
    session.model.calls.clear()
    g.add_cylinder(session, 20, 40)
    assert any(op == "FeatureRevolve2" and len(args) == 20 and args[8] == 2*math.pi for op, args in session.model.calls)
    assert not any(op == "FeatureExtrusion3" for op, _ in session.model.calls)


@pytest.mark.parametrize("top", [0, 5])
def test_cone_and_frustum_have_native_revolve_profiles(session, top):
    assert g.add_cone(session, 20, top, 40)["ok"]
    assert any(op == "FeatureRevolve2" for op, _ in session.model.calls)


def test_revolve_profile_crossing_axis_is_rejected_without_edits(session):
    with pytest.raises(CadError, match="cannot cross"):
        g.add_revolved_profile(session, [[-10, 0], [10, 0], [10, 20], [-10, 20]])
    assert not session.model.calls


def test_rounded_rectangle_has_exact_tangent_lengths_and_arc_midpoints():
    pieces = g.rounded_polyline([[0, 0], [30, 0], [30, 20], [0, 20]], 2)
    assert len(pieces) == 8
    lines = [p for p in pieces if p['kind'] == 'line']
    assert sorted(round(g._norm(g._sub(p['end'], p['start'])), 8) for p in lines) == [16, 16, 26, 26]
    first_arc = next(p for p in pieces if p['kind'] == 'arc')
    assert first_arc['start'] == pytest.approx([28, 0])
    assert first_arc['end'] == pytest.approx([30, 2])
    assert first_arc['through'] == pytest.approx([28+math.sqrt(2), 2-math.sqrt(2)])


def test_oversized_radius_and_bad_radius_count_fail_before_cad(session):
    for radius in (20, [1, 2]):
        with pytest.raises(CadError):
            g.add_extruded_profile(session, [[0, 0], [10, 0], [10, 10], [0, 10]], 3, corner_radii_mm=radius)
    assert not session.model.calls


def test_3d_path_rounding_stays_in_its_path_plane():
    pieces = g.rounded_polyline([[0, 0, 0], [20, 0, 0], [20, 0, 30]], 2, closed=False)
    assert len(pieces) == 3
    assert all(p[1] == 0 for piece in pieces for key, p in piece.items() if key in {'start', 'end', 'through'})
    assert pieces[1]['start'] == pytest.approx([18, 0, 0])
    assert pieces[1]['end'] == pytest.approx([20, 0, 2])


def test_profile_rotation_is_about_the_requested_point():
    points = g.rotated_points([[10, 10], [20, 10], [20, 20]], 90, [10, 10])
    assert points[0] == pytest.approx([10, 10])
    assert points[1] == pytest.approx([10, 20])
    assert points[2] == pytest.approx([0, 20])


def test_face_hole_projects_along_normal_and_uses_global_mm_coordinates(session):
    result = g.add_hole_on_face(session, 8, '+z', 15, 20, 0, depth_mm=3)
    circle = next(args for op, args in session.model.calls if op == 'CreateCircleByRadius')
    cut = next(args for op, args in session.model.calls if op == 'FeatureCut4')
    assert circle == (.015, .02, 0., .004)
    assert cut[5] == .003 and cut[3] == 0
    assert result['center_mm'] == [15., 20., 10.]


def test_face_point_tangential_snap_is_rejected(session):
    session.model.face.GetClosestPointOn = lambda x, y, z: [x+.01, y, .01]
    with pytest.raises(CadError, match='outside'):
        g.add_hole_on_face(session, 8, '+z', 15, 20, 0)
    assert not session.model.calls


def test_hole_normal_projection_does_not_require_a_surface_z_argument(session):
    resolutions = []
    def resolve(reference, point_mm=None):
        resolutions.append((reference, point_mm))
        if point_mm is not None:
            raise CadError('ENTITY_NOT_FOUND', 'The center is ten millimetres below the top face.')
        return session.model.face
    session.resolve_face = resolve
    result = g.add_hole(session, 6, 20, 30)
    assert result['center_mm'] == [20, 30, 10]
    assert resolutions == [('+z', None)]


def test_counterbore_keeps_center_over_the_existing_clearance_void(session):
    def nearest(x, y, z):
        already_cut = any(op == 'FeatureCut4' for op, _ in session.model.calls)
        return [x+.003 if already_cut else x, y, .01]
    session.model.face.GetClosestPointOn = nearest
    result = g.add_counterbore_hole(session, 6, 12, 3, 20, 30)
    circles = [args for op, args in session.model.calls if op == 'CreateCircleByRadius']
    cuts = [args for op, args in session.model.calls if op == 'FeatureCut4']
    assert circles == [(.02, .03, 0., .003), (.02, .03, 0., .006)]
    assert cuts[0][3] == 1 and cuts[1][5] == .003
    assert result['center_mm'] == [20, 30, 10]


def test_xy_cut_profile_uses_model_to_face_sketch_transform(session, monkeypatch):
    original = MathPoint.MultiplyTransform
    monkeypatch.setattr(MathPoint, 'MultiplyTransform', lambda self, transform:
        MathPoint([-self.ArrayData[0], self.ArrayData[1], self.ArrayData[2]-transform.offset]))
    lines = []
    create = session.model.SketchManager.CreateLine
    session.model.SketchManager.CreateLine = lambda *args: (lines.append(args), create(*args))[1]
    g.cut_profile(session, [[10, 10], [30, 10], [30, 30], [10, 30]])
    assert lines[0] == (-.01, .01, 0., -.03, .01, 0.)
    monkeypatch.setattr(MathPoint, 'MultiplyTransform', original)


def test_associative_circle_edge_offsets_create_construction_references(session):
    result = g.add_boss_on_face(session, 10, '+z', 15, 20, 0, 4, from_edges=True)
    assert len(result['measured_from']) == 2
    assert sorted(ref['offset_mm'] for ref in result['measured_from']) == pytest.approx([15, 20])
    assert result['dimensions']['edge_offset_1'].startswith('edge_offset_1@')


def test_plane_offset_and_negative_direction_use_native_constraint_flags(session):
    result = g.add_plane(session, offset_mm=-12, name='Under')
    args = next(args for op, args in session.model.calls if op == 'InsertRefPlane')
    assert args == (8 | 256, .012, 0, 0., 0, 0.) and result['feature'] == 'Under'


def test_native_void_rib_is_verified_from_the_new_feature_tree(session):
    result = g.add_rib(session, [10, 40], [40, 10], [10, 10], 4, 5)
    assert result['feature'] == 'Rib'
    assert session.find_feature('Rib').GetTypeName2() == 'Rib'
    args = next(args for op, args in session.model.calls if op == 'InsertRib')
    assert args[:3] == (True, False, .004)
    assert args[4] is False
    assert ('SelectRibBody', (True, 1)) in session.model.calls


def test_rejected_void_rib_cannot_reuse_an_existing_rib_feature(session):
    Feature(session.model, 'Rib', 'ExistingRib')
    session.model.FeatureManager.reject.add('InsertRib')
    with pytest.raises(CadError, match='new Rib feature'):
        g.add_rib(session, [10, 40], [40, 10], [10, 10], 4, 5)


def test_loft_height_preflight_cannot_leave_planes_on_late_invalid_section(session):
    with pytest.raises(CadError):
        g.add_lofted_solid(session, [[[0, 0], [10, 0], [10, 10]], {'center_mm': [0, 0], 'diameter_mm': -1}], [0, 20])
    assert not session.model.calls


def test_round_and_polygon_loft_sections_select_marked_native_sketches(session):
    result = g.add_lofted_solid(session, [
        {'center_mm': [0, 0], 'diameter_mm': 20},
        [[-5, -5], [5, -5], [5, 5], [-5, 5]],
    ], [0, 25])
    assert result['ok'] and len(result['section_sketches']) == 2
    assert [mark for _, mark in session.model.selected] == [1, 1]
    assert any(op == 'InsertProtrusionBlend2' and len(args) == 18 for op, args in session.model.calls)


@pytest.mark.parametrize('smooth', [False, True])
def test_native_circular_sweep_uses_path_mark_and_actual_diameter(session, smooth):
    result = g.add_swept_pipe(session, [[0, 0], [30, 0], [30, 30]], 6,
                              bend_radius_mm=0 if smooth else 8, smooth=smooth)
    args = next(args for op, args in session.model.calls if op == 'InsertProtrusionSwept4')
    assert result['ok'] and len(args) == 20 and args[17:19] == (True, .006)
    assert session.model.selected[-1][1] == 4


def test_smooth_path_cannot_silently_ignore_an_exact_bend_radius(session):
    with pytest.raises(CadError) as caught:
        g.add_swept_pipe(session, [[0, 0], [30, 0], [30, 30]], 6, bend_radius_mm=8, smooth=True)
    assert caught.value.code == 'CONFLICTING_PATH_CONSTRAINTS'
    assert not session.model.calls


def test_sweep_profile_has_native_path_normal_plane(session):
    result = g.add_swept_profile(session, [[-2, -2], [2, -2], [2, 2], [-2, 2]], [[0, 0], [30, 0]])
    assert result['ok']
    plane = next(args for op, args in session.model.calls if op == 'InsertRefPlane')
    assert plane == (2, 0., 4, 0., 0, 0.)
    assert [mark for _, mark in session.model.selected] == [1, 4]


def test_slot_piece_geometry_is_closed_and_has_real_semicircles():
    pieces = g.slot_outline([0, 0], [20, 0], 8)
    assert pieces[0]['start'] == [0, 4]
    assert pieces[-1]['end'] == pieces[0]['start']
    assert pieces[1]['through'] == [24, 0]
    assert pieces[3]['through'] == [-4, 0]


@pytest.mark.parametrize('operation', ['add', 'cut'])
def test_native_slot_builds_lines_arcs_and_solid_or_cut(session, operation):
    result = g.add_extruded_slot(session, [0, 0], [30, 0], 8, 4) if operation == 'add' else g.cut_slot(session, 38, 8, 20, 20)
    assert result['ok']
    assert sum(op == 'Create3PointArc' for op, _ in session.model.calls) == 2
    assert any(op == ('FeatureExtrusion3' if operation == 'add' else 'FeatureCut4') for op, _ in session.model.calls)


def test_mixed_sketch_arc_through_point_and_closed_status(session):
    result = g.add_sketch(session, 'front', [0, 0], [
        {'line': [20, 0]}, {'arc': [20, 20], 'through': [30, 10]},
        {'line': [0, 20]}, {'line': [0, 0]},
    ], name='Mixed')
    assert result['closed'] and result['segment_count'] == 4 and result['sketch'] == 'Mixed'
    assert session.model.active is None


def test_requested_line_tangency_is_checked_before_native_edits(session):
    with pytest.raises(CadError, match='not tangent'):
        g.add_sketch(session, 'front', [0, 0], [{'line': [10, 0]}, {'line': [10, 10], 'tangent': True}])
    assert not session.model.calls


def test_through_point_arc_rejects_a_false_tangency_claim_before_cad(session):
    with pytest.raises(CadError, match='not tangent'):
        g.add_sketch(session, 'front', [0, 0], [{'line': [10, 0]},
            {'arc': [20, 10], 'through': [20, 0], 'tangent': True}])
    assert not session.model.calls


def test_through_point_arc_checks_its_starting_tangent(session):
    result = g.add_sketch(session, 'front', [-10, 0], [{'line': [0, 0]},
        {'arc': [10, 10], 'through': [math.sqrt(50), 10-math.sqrt(50)], 'tangent': True}])
    assert result['segment_count'] == 2


def test_unimplemented_spline_tangency_is_rejected_before_cad(session):
    with pytest.raises(CadError) as caught:
        g.add_sketch(session, 'front', [0, 0], [{'line': [10, 0]},
            {'spline': [[20, 0], [30, 10]], 'tangent': True}])
    assert caught.value.code == 'SPLINE_TANGENCY_UNSUPPORTED'
    assert not session.model.calls


def test_tangent_arc_midpoint_matches_starting_tangent():
    midpoint = g._tangent_midpoint([0, 0], [10, 10], [1, 0])
    assert midpoint == pytest.approx([math.sqrt(50), 10-math.sqrt(50)])
    assert g._arc_tangent([0, 0], midpoint, [10, 10]) == pytest.approx([0, 1])


def test_text_creation_sets_and_reads_native_character_height(session):
    result = g.add_text_on_face(session, 'PRO', '+z', 20, 30, 0, 5, 1, emboss=True)
    assert result['ok'] and any(op == 'FeatureExtrusion3' for op, _ in session.model.calls)


def test_hole_wizard_uses_native_iso_tables_and_verified_location(session):
    selections = []
    session.model.Extension.SelectByID2 = lambda *args: (selections.append(args), True)[1]
    result = g.add_hole_wizard(session, 'clearance', 'M6', '+z', 20, 30, 0)
    args = next(args for op, args in session.model.calls if op == 'HoleWizard5')
    assert len(args) == 27 and args[:5] == (2, 8, 144, 'M6', 1)
    assert result['hole']['diameter_mm'] == 6
    assert result['center_mm'] == [20, 30, 10]
    assert selections[0][7] is None
    assert session.model.active is None
    assert ('ForceRebuild3', (False,)) in session.model.calls


def test_native_wizard_rejection_does_not_fall_back_to_an_approximate_hole(session):
    session.model.FeatureManager.reject.add('HoleWizard5')
    with pytest.raises(CadError, match='rejected'):
        g.add_hole_wizard(session, 'tapped', 'M99', '+z', 20, 30, 0)
    assert not any(op == 'FeatureCut4' for op, _ in session.model.calls)


def test_dimension_dialog_setting_is_restored_if_dimension_creation_raises(session):
    def reject(*args):
        raise RuntimeError('native dimension failure')
    session.model.AddDimension2 = reject
    with pytest.raises(RuntimeError, match='dimension failure'):
        g.add_disc(session, 20, 5)
    assert session.prompt and session.model.active is None
    assert session.preferences == [(10, False), (10, True)]


def test_all_thirty_public_geometry_handlers_are_distinct_implementations():
    assert len(g.TOOLS) == 30
    assert len({function.__code__ for function in g.TOOLS.values()}) == 30
    assert all(function.__module__ == 'solidworks_mcp_pro.native.geometry' for function in g.TOOLS.values())


def test_existing_user_sketch_is_not_closed_or_replaced(session):
    existing = Sketch(session.model)
    session.model.active = existing
    with pytest.raises(CadError, match='existing sketch'):
        g.add_disc(session, 20, 5)
    assert session.model.active is existing


def test_profile_on_offset_plane_rejects_off_plane_model_points(session):
    plane = Feature(session.model, 'RefPlane', 'Offset')
    plane.offset = .02
    with pytest.raises(CadError, match='must lie'):
        g.add_extruded_profile_on_plane(session, [[0, 0, 0], [10, 0, 0], [10, 10, 0]], 'Offset', 3)
    assert not any(name == 'FeatureExtrusion3' for name, _ in session.model.calls)
    assert session.model.active is None


def test_shape_interface_is_reacquired_as_selectable_sketch_segment(session, monkeypatch):
    _, manager, _, _ = g.start_sketch(session)
    selectable = manager.CreateLine(0., 0., 0., .1, 0., 0.)
    narrow = object()
    original = g.binding.wrap
    seen = []
    def wrap(value, interface):
        if value is narrow:
            seen.append(interface)
            assert interface == 'ISketchSegment'
            return selectable
        return original(value, interface)
    monkeypatch.setattr(g.binding, 'wrap', wrap)
    result = g._named_dimension(session, session.model, narrow, 100, [50, -20], 'width')
    assert result.startswith('width@') and seen == ['ISketchSegment']


def test_bad_draft_or_non_boolean_merge_has_no_native_edits(session):
    for kwargs in ({'draft_deg': 90}, {'merge': 1}, {'reverse': 'yes'}):
        with pytest.raises(CadError):
            g.extrude_profile(session, [[0, 0], [10, 0], [10, 10]], 3, **kwargs)
    assert not session.model.calls


def test_up_to_face_uses_explicit_native_end_reference(session):
    sketch = Feature(session.model, 'ProfileFeature', 'ExistingSketch')
    result = g.extrude_sketch(session, sketch.Name, up_to='+z')
    args = next(args for name, args in session.model.calls if name == 'FeatureExtrusion3')
    assert result['ok'] and args[3] == 4 and args[5] == 0
    assert [mark for _, mark in session.model.selected] == [0, 1]


def test_angle_and_offset_plane_does_not_silently_discard_offset(session):
    Feature(session.model, 'RefAxis', 'Hinge')
    g.add_plane(session, offset_mm=5, angle_deg=30, about='Hinge')
    calls = [args for name, args in session.model.calls if name == 'InsertRefPlane']
    assert len(calls) == 2 and calls[0][0:2] == (8, .005)
    assert calls[1][0:4] == pytest.approx([16, math.pi/6, 4, 0])


def test_dimension_preference_restore_failure_is_explicit(session):
    def change(index, value):
        if not value:
            session.prompt = False
    session.SetUserPreferenceToggle = change
    with pytest.raises(CadError, match='could not be restored') as caught:
        g.add_disc(session, 20, 5)
    assert caught.value.code == 'DIMENSION_PROMPT_RESTORE_FAILED'
    assert session.model.active is None


def test_sketch_creation_exception_restores_addtodb_and_ends_owned_edit(session):
    def reject(*args):
        raise RuntimeError('native line failure')
    session.model.SketchManager.CreateLine = reject
    with pytest.raises(RuntimeError, match='line failure'):
        g.add_extruded_profile(session, [[0, 0], [10, 0], [10, 10]], 3)
    assert session.model.active is None and session.model.SketchManager.AddToDB is False
