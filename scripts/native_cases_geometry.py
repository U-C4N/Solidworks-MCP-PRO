"""Fresh-part native recipes for the 30 independent geometry tools.

The external runner creates an empty owned part for each case. Numeric volume
checks come from elementary analytic geometry, not from the engine's result.
No COM or CAD calls occur while importing this module.
"""
from math import pi


def _plate(width=80, height=50, depth=10):
    return [{"tool": "add_box", "args": {"width_mm": width, "height_mm": height,
             "depth_mm": depth, "name": "FixturePlate"}, "bind": "base"}]


def _sketch(name, plane="front", size=20):
    return {"tool": "add_sketch", "args": {"plane": plane, "start_mm": [0, 0],
        "segments": [{"line": [size, 0]}, {"line": [size, size]},
                     {"line": [0, size]}, {"line": [0, 0]}], "name": name}}


def _case(tool, args, *, setup=None, volume=None):
    case = {"id": tool, "setup": [] if setup is None else setup, "tool": tool, "args": args}
    if volume is not None:
        case["check"] = {"volume_mm3": volume}
    return case


CASES = [
    _case("add_box", {"width_mm": 80, "height_mm": 50, "depth_mm": 10,
                      "name": "TestBox"}, volume=40000),
    _case("add_extruded_profile", {"points_mm": [[0, 0], [40, 0], [0, 30]],
        "depth_mm": 10, "name": "Triangle"}, volume=6000),
    _case("add_extruded_spline", {"points_mm": [[0, 10], [10, 0], [20, 10], [10, 20]],
        "depth_mm": 8, "name": "ClosedSpline"}),
    _case("add_disc", {"diameter_mm": 20, "thickness_mm": 5,
        "x_mm": 10, "y_mm": 10, "name": "TestDisc"}, volume=pi*100*5),
    _case("add_cylinder", {"diameter_mm": 20, "height_mm": 30,
        "name": "TestCylinder"}, volume=pi*100*30),
    _case("add_cone", {"bottom_diameter_mm": 20, "top_diameter_mm": 0,
        "height_mm": 30, "name": "TestCone"}, volume=pi*100*30/3),
    _case("add_revolved_profile", {"profile_mm": [[0, 0], [10, 0], [10, 30], [0, 30]],
        "angle_deg": 360, "name": "TestRevolve"}, volume=pi*100*30),
    _case("add_swept_pipe", {"path_mm": [[0, 0], [40, 0]], "diameter_mm": 6,
        "name": "StraightRod"}, volume=pi*9*40),
    _case("add_swept_profile", {"profile_mm": [[-2, -2], [2, -2], [2, 2], [-2, 2]],
        "path_mm": [[0, 0], [40, 0]], "name": "SquareRail"}, volume=16*40),
    _case("add_lofted_solid", {"profiles_mm": [
        [[0, 0], [20, 0], [20, 20], [0, 20]],
        [[0, 0], [20, 0], [20, 20], [0, 20]]], "heights_mm": [0, 30],
        "name": "PrismaticLoft"}, volume=20*20*30),
    _case("loft_sketches", {"sketches": ["LowerSection", "UpperSection"],
        "name": "SketchLoft"}, setup=[
            {"tool": "add_plane", "args": {"base": "front", "offset_mm": 30,
                                             "name": "UpperPlane"}},
            _sketch("LowerSection"), _sketch("UpperSection", "UpperPlane")],
        volume=20*20*30),
    _case("cut_profile_through_plane", {"points_mm": [[10, 10], [30, 10], [30, 30], [10, 30]],
        "plane": "front", "name": "ThroughWindow"}, setup=_plate(), volume=40000-20*20*10),
    _case("add_plane", {"base": "front", "offset_mm": 20,
        "name": "RaisedPlane"}, setup=_plate(), volume=40000),
    _case("add_extruded_profile_on_plane", {"points_mm": [[0, 0], [40, 0], [40, 30], [0, 30]],
        "plane": "RaisedPlane", "depth_mm": 8, "name": "RaisedSolid"}, setup=[
            {"tool": "add_plane", "args": {"base": "front", "offset_mm": 20,
                                             "name": "RaisedPlane"}}], volume=40*30*8),
    _case("add_rib", {"start_mm": [10, 40], "end_mm": [40, 10], "toward_mm": [10, 10],
        "thickness_mm": 4, "z_mm": 5, "name": "CornerGusset"}, setup=_plate(50, 50, 10)+[
            {"tool": "cut_profile", "args": {"points_mm": [[10, 10], [50, 10], [50, 50], [10, 50]],
                                              "name": "LCorner"}}], volume=9000+30*30/2*4),
    _case("add_hole", {"diameter_mm": 8, "x_mm": 20, "y_mm": 20,
        "name": "MountingHole"}, setup=_plate(), volume=40000-pi*16*10),
    _case("add_counterbore_hole", {"clearance_diameter_mm": 6, "cbore_diameter_mm": 12,
        "cbore_depth_mm": 3, "x_mm": 20, "y_mm": 20,
        "name": "SocketSeat"}, setup=_plate(), volume=40000-pi*9*10-pi*(36-9)*3),
    _case("add_hole_on_face", {"diameter_mm": 8, "face": "+z", "x_mm": 20,
        "y_mm": 20, "z_mm": 10, "depth_mm": 3,
        "name": "BlindHole"}, setup=_plate(), volume=40000-pi*16*3),
    _case("add_hole_wizard", {"kind": "clearance", "size": "M6", "face": "+z",
        "x_mm": 20, "y_mm": 20, "z_mm": 10, "fit": "normal", "name": "IsoClearance"},
        setup=_plate()),
    _case("add_boss_on_face", {"diameter_mm": 10, "face": "+z", "x_mm": 20,
        "y_mm": 20, "z_mm": 10, "height_mm": 5,
        "name": "Standoff"}, setup=_plate(), volume=40000+pi*25*5),
    _case("add_extruded_profile_on_face", {"points_mm": [
        [20, 10, 10], [40, 10, 10], [40, 30, 10], [20, 30, 10]],
        "face": "+z", "depth_mm": 5, "name": "SquareBoss"}, setup=_plate(), volume=42000),
    _case("add_text_on_face", {"text": "PRO", "face": "+z", "x_mm": 15,
        "y_mm": 15, "z_mm": 10, "height_mm": 5, "depth_mm": 1,
        "emboss": False, "font": "Arial", "name": "Engraving"}, setup=_plate()),
    _case("cut_profile", {"points_mm": [[10, 10], [30, 10], [30, 30], [10, 30]],
        "name": "SquareWindow"}, setup=_plate(), volume=36000),
    _case("cut_profile_on_face", {"points_mm": [
        [80, 10, 2], [80, 30, 2], [80, 30, 8], [80, 10, 8]], "face": "+x",
        "depth_mm": 20, "name": "SidePocket"}, setup=_plate(), volume=40000-20*6*20),
    _case("cut_offset_pocket", {"face": "+z", "x_mm": 40, "y_mm": 25,
        "z_mm": 10, "rim_mm": 5, "depth_mm": 3,
        "name": "RimPocket"}, setup=_plate(), volume=40000-70*40*3),
    _case("cut_slot", {"length_mm": 30, "width_mm": 8, "x_mm": 40,
        "y_mm": 25, "name": "ThroughSlot"}, setup=_plate(), volume=40000-(22*8+pi*16)*10),
    _case("add_extruded_slot", {"start_mm": [10, 10], "end_mm": [40, 10],
        "width_mm": 8, "depth_mm": 10,
        "name": "SlotSolid"}, volume=(30*8+pi*16)*10),
    _case("extrude_sketch", {"sketch": "ExtrudeSection", "depth_mm": 10,
        "name": "SketchSolid"}, setup=[_sketch("ExtrudeSection")], volume=4000),
    _case("add_sketch", {"plane": "top", "start_mm": [0, 0], "segments": [
        {"line": [20, 0]}, {"line": [20, 20]}, {"line": [0, 20]}, {"line": [0, 0]}],
        "name": "ReferenceOutline"}, setup=_plate(), volume=40000),
    _case("cut_sketch", {"sketch": "CutSection", "reverse": True,
        "name": "SketchWindow"}, setup=_plate()+[_sketch("CutSection")], volume=36000),
]
