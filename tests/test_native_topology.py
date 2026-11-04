from types import SimpleNamespace as NS

import pytest

from solidworks_mcp_pro.native import binding, topology as api
from solidworks_mcp_pro.native.errors import CadError
from test_native_assembly import PartModel, Session, Face


class Edge:
    def __init__(self, start, end):
        self.start, self.end = start, end
        self.curve = NS(
            IsLine=lambda: True,
            IsCircle=lambda: False,
            GetLength3=lambda a, b: sum((x - y) ** 2 for x, y in zip(start, end)) ** 0.5,
        )
        self.selected = []

    def GetCurve(self):
        return self.curve

    def GetCurveParams3(self):
        return NS(UMinValue=0.0, UMaxValue=1.0)

    def GetStartVertex(self):
        return NS(GetPoint=lambda: self.start)

    def GetEndVertex(self):
        return NS(GetPoint=lambda: self.end)

    def Select4(self, append, data):
        self.selected.append((append, data.Mark))
        return True


@pytest.fixture
def session():
    model = PartModel()
    edges = [
        Edge([0.0, 0.0, 0.0], [0.01, 0.0, 0.0]),
        Edge([0.01, 0.0, 0.0], [0.01, 0.01, 0.0]),
        Edge([0.0, 0.0, 0.0], [0.0, 0.0, 0.01]),
        Edge([0.0, 0.0, 0.0], [0.001, 0.0, 0.0]),
    ]
    model.bodies[0].GetEdges = lambda: edges
    model.bodies[0].faces[-1].edges = edges[:2]
    return Session(model)


def test_faces_report_native_units_normals_and_expiring_ids(session):
    result = api.list_faces(session)
    assert result["count"] == 6 and result["faces"][0]["area_mm2"] == 100
    assert result["faces"][-1]["normal"] == [0, 0, 1]
    token = result["faces"][-1]["id"]
    assert api.resolve(session, token) is session.model.bodies[0].faces[-1]
    session.model.stamp += 1
    with pytest.raises(CadError, match="STALE_ENTITY_REFERENCE"):
        api.resolve(session, token)
    assert api.list_faces(session)["reference_epoch"] == 2


def test_configuration_change_invalidates_references_without_stamp_change(session):
    token = api.list_faces(session)["faces"][0]["id"]
    session.model.GetActiveConfiguration = lambda: NS(Name="Other")
    with pytest.raises(CadError, match="STALE_ENTITY_REFERENCE"):
        api.resolve(session, token)


def test_forced_cache_clear_does_not_reissue_an_old_token(session):
    old_token = api.list_faces(session)["faces"][0]["id"]
    session._topology_cache = {}
    new_token = api.list_faces(session)["faces"][0]["id"]
    assert old_token != new_token
    with pytest.raises(CadError, match="STALE_ENTITY_REFERENCE"):
        api.resolve(session, old_token)


def test_direction_alias_and_explicit_pick_point_select_planar_face(session):
    expected = session.model.bodies[0].faces[-1]
    assert api.resolve(session, "+z") is expected
    assert api.resolve(session, "front") is expected
    assert api.resolve(session, "+z", point_mm=[5, 5, 10]) is expected
    assert api.resolve(session, "#6") is expected


def test_point_tolerance_prevents_far_nearest_face_matches(session):
    with pytest.raises(CadError, match="ENTITY_NOT_FOUND"):
        api.resolve(session, "+z", point_mm=[50, 50, 10])


def test_duplicate_planar_faces_require_explicit_disambiguation(session):
    session.model.bodies[0].faces.append(Face([0, 0, 1], [0.02, 0, 0.01], [0.03, 0.01, 0.01]))
    with pytest.raises(CadError, match="ENTITY_AMBIGUOUS"):
        api.resolve(session, "+z")
    assert api.resolve(session, "+z", point_mm=[25, 5, 10]) is session.model.bodies[0].faces[-1]


def test_bad_reference_and_type_cannot_select_anything(session):
    face_id = api.list_faces(session)["faces"][0]["id"]
    with pytest.raises(CadError, match="ENTITY_SCOPE_MISMATCH"):
        api.resolve(session, face_id, kind="edge")
    with pytest.raises(CadError, match="ENTITY_NOT_FOUND"):
        api.resolve(session, "unrelated-name")
    with pytest.raises(CadError):
        api.resolve(session, 0)


def test_edges_report_native_lengths_and_axis_selectors(session):
    result = api.list_edges(session)
    assert result["count"] == 4 and result["edges"][0]["length_mm"] == 10
    assert result["edges"][0]["direction"] == [1, 0, 0]
    assert len(api.resolve_edges(session, "vertical")) == 1
    assert len(api.resolve_edges(session, "horizontal")) == 3
    assert len(api.resolve_edges(session, "x")) == 2


def test_filter_preserves_inventory_indices_and_minimum_length(session):
    result = api.list_edges(session, min_length_mm=5)
    assert len(result["edges"]) == 3 and result["edges"][-1]["index"] == 3
    assert len(api.resolve_edges(session, "all", skip_shorter_mm=5)) == 3
    assert api.resolve_edges(session, "1,3,1") == [session.model.bodies[0].GetEdges()[i] for i in [0, 2]]


def test_filtered_first_edge_does_not_renumber_later_native_edge(session):
    values = session.model.bodies[0].GetEdges()
    session.model.bodies[0].GetEdges = lambda: [values[-1], values[0]]
    result = api.list_edges(session, min_length_mm=5)
    assert result["edges"][0]["index"] == 2
    assert api.resolve(session, 2, kind="edge") is values[0]


def test_face_and_feature_filters_keep_inventory_indices(session):
    edges = session.model.bodies[0].GetEdges()
    face = session.model.bodies[0].faces[-1]
    face.edges = [edges[1], edges[2]]
    session.find_feature = lambda name: NS(GetFaces=lambda: [face])
    for query in ({"face": "+z"}, {"feature": "Boss"}):
        result = api.list_edges(session, **query)
        assert [record["index"] for record in result["edges"]] == [2, 3]
        assert api.resolve(session, result["edges"][0]["index"], kind="edge") is edges[1]


def test_edge_region_filter_requires_entire_line_inside_box(session):
    result = api.list_edges(session, within_mm=[0, 0, -1, 2, 2, 1])
    assert result["count"] == 1 and result["edges"][0]["length_mm"] == 1


def test_public_nested_region_box_matches_flat_internal_box(session):
    nested = api.list_edges(session, within_mm=[[0, 0, -1], [2, 2, 1]])
    flat = api.list_edges(session, within_mm=[0, 0, -1, 2, 2, 1])
    assert nested["edges"] == flat["edges"] and nested["count"] == 1


def test_circle_region_filter_uses_entire_supporting_circle_not_endpoints(session):
    edge = session.model.bodies[0].GetEdges()[0]
    edge.curve = NS(
        IsLine=lambda: False,
        IsCircle=lambda: True,
        GetLength3=lambda a, b: 0.0314159,
        CircleParams=[0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.005],
    )
    edge.GetStartVertex = lambda: None
    edge.GetEndVertex = lambda: None
    session.model.bodies[0].GetEdges = lambda: [edge]
    assert api.list_edges(session, within_mm=[-5, -5, -1, 5, 5, 1])["count"] == 1
    assert api.list_edges(session, within_mm=[-1, -1, -1, 1, 1, 1])["count"] == 0


def test_unknown_curve_region_filter_is_explicitly_unsupported(session):
    edge = session.model.bodies[0].GetEdges()[0]
    edge.curve.IsLine = lambda: False
    with pytest.raises(CadError, match="EDGE_REGION_UNSUPPORTED"):
        api.list_edges(session, within_mm=[0, 0, 0, 20, 20, 20])


@pytest.mark.parametrize("query", [{"within_mm": [1, 1, 1, 0, 0, 0]}, {"min_length_mm": -1}])
def test_invalid_edge_filters_fail_before_results(session, query):
    with pytest.raises(CadError, match="INVALID_ARGUMENT"):
        api.list_edges(session, **query)


def test_feature_selector_uses_native_face_adjacency(session):
    face = session.model.bodies[0].faces[-1]
    session.find_feature = lambda name: NS(GetFaces=lambda: [face]) if name == "Boss" else None
    assert api.resolve_edges(session, "feature:Boss") == face.edges


def test_outline_selector_excludes_inner_hole_loop(session):
    face = session.model.bodies[0].faces[-1]
    inner = Edge([0.004, 0.004, 0.01], [0.006, 0.004, 0.01])
    face.GetLoops = lambda: [
        NS(IsOuter=lambda: True, GetEdges=lambda: face.edges),
        NS(IsOuter=lambda: False, GetEdges=lambda: [inner]),
    ]
    assert api.resolve_edges(session, "+z:outline") == face.edges


def test_selection_uses_native_entity_query_interface_and_selection_mark(session, monkeypatch):
    requested = []
    original = binding.wrap

    def wrap(obj, interface):
        requested.append(interface)
        return original(obj, interface)

    monkeypatch.setattr(binding, "wrap", wrap)
    face = api.select(session, "+z", "face", append=True, mark=16)
    assert "IEntity" in requested and face.selected == [(True, 16)]


def test_circular_edge_length_and_radius_from_native_curve(session):
    edge = session.model.bodies[0].GetEdges()[0]
    edge.curve = NS(
        IsLine=lambda: False,
        IsCircle=lambda: True,
        GetLength3=lambda a, b: 0.0314159,
        CircleParams=[0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.005],
    )
    edge.GetStartVertex = lambda: None
    edge.GetEndVertex = lambda: None
    record = api.list_edges(session)["edges"][0]
    assert (
        record["type"] == "circle"
        and record["radius_mm"] == 5
        and record["length_mm"] == pytest.approx(31.4159)
    )


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), 10**400])
def test_invalid_topology_numbers_are_rejected(value):
    with pytest.raises(CadError):
        api.number(value)
