"""Exercise mechanical operations against real OCCT shapes and semantic references."""

import copy
import math
from uuid import uuid4

import pytest
from deep3d_cad.contracts import CadError, PrecisionProfile
from deep3d_cad.features import (
    _compound,
    _solid,
    _solid_count,
    _volume,
    execute_feature,
    validate_feature,
)
from deep3d_cad.kernel import _brep_bytes, read_brep, write_brep
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps


class Graph:
    def __init__(self):
        self.shapes, self.types = {}, {}

    def add(self, op, parameters=None, inputs=None, data=None):
        feature = {
            "id": str(uuid4()),
            "name": op,
            "op": op,
            "parameters": parameters or {},
            "inputs": inputs or {},
        }
        if data is not None:
            feature["data"] = data
        result_type = validate_feature(feature, self.types)
        shape, report = execute_feature(feature, self.shapes, PrecisionProfile())
        self.shapes[feature["id"]], self.types[feature["id"]] = shape, result_type
        return feature["id"], shape, report

    def box(self, width=20, height=10, depth=5, x=0, y=0):
        profile, _, _ = self.add(
            "profile.rectangle@1",
            {"width_mm": width, "height_mm": height, "center_x_mm": x, "center_y_mm": y},
        )
        return self.add("solid.extrude@1", {"distance_mm": depth}, {"profile": profile})


def transform(dx=0, dy=0, dz=0, angle=0):
    return {
        "dx_mm": dx,
        "dy_mm": dy,
        "dz_mm": dz,
        "angle_rad": angle,
        "axis_x": 0,
        "axis_y": 0,
        "axis_z": 1,
    }


def origin_axis(angle=math.tau):
    return {
        "angle_rad": angle,
        "origin_x_mm": 0,
        "origin_y_mm": 0,
        "origin_z_mm": 0,
        "axis_x": 0,
        "axis_y": 1,
        "axis_z": 0,
    }


def test_rectangle_polygon_and_revolution_remain_real_serializable_breps(tmp_path):
    graph = Graph()
    _, box, _ = graph.box()
    assert _volume(box) == pytest.approx(1000)
    profile, _, _ = graph.add("profile.polygon@1", data={"points_mm": [[0, 0], [4, 0], [0, 3]]})
    _, triangle, _ = graph.add("solid.extrude@1", {"distance_mm": 10}, {"profile": profile})
    assert _volume(triangle) == pytest.approx(60)
    profile, _, _ = graph.add(
        "profile.rectangle@1",
        {"width_mm": 5, "height_mm": 20, "center_x_mm": 7.5, "center_y_mm": 0},
    )
    _, tube, _ = graph.add("solid.revolve@1", origin_axis(), {"profile": profile})
    assert _volume(tube) == pytest.approx(1500 * math.pi)
    for name, shape in [("box", box), ("triangle", triangle), ("tube", tube)]:
        path = tmp_path / f"{name}.brep"
        write_brep(shape, path)
        assert _volume(read_brep(path)) == pytest.approx(_volume(shape))


@pytest.mark.parametrize(
    "points",
    [
        [[0, 0], [2, 2], [0, 2], [2, 0]],
        [[0, 0], [1, 0], [2, 0]],
        [[0, 0], [2, 0], [2, 2], [1, 0], [0, 2]],
        [[0, 0], [2, 0], [2, 2], [0, 0]],
    ],
)
def test_polygon_rejects_self_intersection_touching_and_degenerate_edges(points):
    with pytest.raises(CadError):
        Graph().add("profile.polygon@1", data={"points_mm": points})


@pytest.mark.parametrize(
    "op,expected",
    [
        ("solid.cut@1", 500),
        ("solid.fuse@1", 1500),
        ("solid.common@1", 500),
    ],
)
def test_booleans_measure_results_without_mutating_upstream_shapes(op, expected):
    graph = Graph()
    first, base, _ = graph.box()
    second, tool, _ = graph.box(x=10)
    before = _brep_bytes(base), _brep_bytes(tool)
    _, result, report = graph.add(op, inputs={"base": first, "tool": second})
    assert _volume(result) == pytest.approx(expected)
    assert (_brep_bytes(base), _brep_bytes(tool)) == before
    assert report["non_destructive"] is True
    assert report["errors"] == report["warnings"] == []
    assert report["history"]["available"] is True


def test_empty_boolean_and_wrong_geometry_type_are_rejected():
    graph = Graph()
    solid, _, _ = graph.box()
    with pytest.raises(CadError) as failure:
        graph.add("solid.cut@1", inputs={"base": solid, "tool": solid})
    assert failure.value.code == "EMPTY_GEOMETRY"
    profile, _, _ = graph.add(
        "profile.circle@1", {"radius_mm": 2, "center_x_mm": 0, "center_y_mm": 0}
    )
    with pytest.raises(CadError) as failure:
        graph.add("solid.fuse@1", inputs={"base": solid, "tool": profile})
    assert failure.value.code == "TYPE_MISMATCH"


def test_rigid_transform_preserves_volume_and_rotates_then_translates():
    graph = Graph()
    source, original, _ = graph.box(x=4, y=2)
    before = _brep_bytes(original)
    _, changed, _ = graph.add(
        "shape.transform@1", transform(dx=20, angle=math.pi / 2), {"shape": source}
    )
    properties = GProp_GProps()
    BRepGProp.VolumeProperties_s(changed, properties)
    center = properties.CentreOfMass()
    assert center.Coord() == pytest.approx((18, 4, 2.5))
    assert _volume(changed) == pytest.approx(_volume(original))
    assert _brep_bytes(original) == before


@pytest.mark.parametrize(
    "op,size,expected",
    [
        ("solid.fillet@1", {"radius_mm": 1}, 980 + 5 * math.pi),
        ("solid.chamfer@1", {"distance_mm": 1}, 990),
    ],
)
def test_edge_treatments_resolve_semantic_edges_and_preserve_input(op, size, expected):
    graph = Graph()
    source, original, _ = graph.box()
    before = _brep_bytes(original)
    _, result, report = graph.add(
        op,
        size,
        {"shape": source},
        {"selector": {"kind": "line_parallel", "axis": [0, 0, 1], "expected_count": 4}},
    )
    assert _volume(result) == pytest.approx(expected, rel=1e-8)
    assert _brep_bytes(original) == before
    assert report["selection"]["resolved_count"] == 4
    assert all("index" not in str(item) for item in report["selection"]["edges"])


def test_analytical_circle_selection_and_unbuildable_fillet_fail_closed():
    graph = Graph()
    profile, _, _ = graph.add(
        "profile.circle@1", {"radius_mm": 10, "center_x_mm": 0, "center_y_mm": 0}
    )
    solid, original, _ = graph.add("solid.extrude@1", {"distance_mm": 5}, {"profile": profile})
    selector = {
        "kind": "circle",
        "axis": "z",
        "coordinate_mm": 5,
        "radius_mm": 10,
        "expected_count": 1,
    }
    _, result, report = graph.add(
        "solid.fillet@1", {"radius_mm": 1}, {"shape": solid}, {"selector": selector}
    )
    assert _volume(result) < _volume(original)
    assert report["selection"]["resolved_count"] == 1
    with pytest.raises(CadError) as failure:
        graph.add("solid.fillet@1", {"radius_mm": 1000}, {"shape": solid}, {"selector": selector})
    assert failure.value.code in {"GEOMETRY_KERNEL_FAILURE", "GEOMETRY_INVALID", "EMPTY_GEOMETRY"}


@pytest.mark.parametrize(
    "selector,code",
    [
        (
            {"kind": "line_parallel", "axis": [0, 0, 1], "expected_count": 1},
            "TOPOLOGY_REFERENCE_AMBIGUOUS",
        ),
        (
            {
                "kind": "circle",
                "axis": "z",
                "coordinate_mm": 5,
                "radius_mm": 3,
                "expected_count": 1,
            },
            "TOPOLOGY_REFERENCE_MISSING",
        ),
    ],
)
def test_ambiguous_or_missing_edge_references_are_explicit_failures(selector, code):
    graph = Graph()
    source, _, _ = graph.box()
    with pytest.raises(CadError) as failure:
        graph.add("solid.chamfer@1", {"distance_mm": 1}, {"shape": source}, {"selector": selector})
    assert failure.value.code == code


def test_linear_circular_patterns_and_compounds_keep_explicit_instances():
    graph = Graph()
    box, original, _ = graph.box(width=2, height=2, depth=2)
    _, patterned, report = graph.add(
        "shape.linear_pattern@1",
        {"step_x_mm": 10, "step_y_mm": 0, "step_z_mm": 0},
        {"shape": box},
        {"count": 3, "fuse": False},
    )
    assert _solid_count(patterned) == 3
    assert _volume(patterned) == pytest.approx(3 * _volume(original))
    assert report["instance_count"] == 3
    _, fused, _ = graph.add(
        "shape.linear_pattern@1",
        {"step_x_mm": 1, "step_y_mm": 0, "step_z_mm": 0},
        {"shape": box},
        {"count": 3, "fuse": True},
    )
    assert _solid_count(fused) == 1
    assert _volume(fused) == pytest.approx(16)
    profile, _, _ = graph.add(
        "profile.circle@1", {"radius_mm": 1, "center_x_mm": 10, "center_y_mm": 0}
    )
    circle, cylinder, _ = graph.add("solid.extrude@1", {"distance_mm": 5}, {"profile": profile})
    parameters = {
        "origin_x_mm": 0,
        "origin_y_mm": 0,
        "origin_z_mm": 0,
        "axis_x": 0,
        "axis_y": 0,
        "axis_z": 1,
        "step_angle_rad": math.pi / 2,
    }
    patterned_id, patterned, _ = graph.add(
        "shape.circular_pattern@1", parameters, {"shape": circle}, {"count": 4, "fuse": False}
    )
    assert _solid_count(patterned) == 4
    assert _volume(patterned) == pytest.approx(4 * _volume(cylinder))
    _, compound, _ = graph.add(
        "assembly.compound@1", inputs={"part_0": box, "part_1": patterned_id}
    )
    assert _solid_count(compound) == 5


def test_compound_repeated_definition_keeps_two_occurrences():
    graph = Graph()
    source, original, _ = graph.box()
    before = _brep_bytes(original)
    _, assembled, report = graph.add(
        "assembly.compound@1", inputs={"part_0": source, "part_1": source}
    )
    assert _solid_count(assembled) == 2
    assert _volume(assembled) == pytest.approx(2 * _volume(original))
    assert report["part_count"] == 2
    assert _brep_bytes(original) == before


def test_solid_category_rejects_loose_surfaces_and_individually_inverted_solids():
    graph = Graph()
    _, large, _ = graph.box()
    _, small, _ = graph.box(width=2, height=2, depth=2)
    _, face, _ = graph.add(
        "profile.rectangle@1", {"width_mm": 1, "height_mm": 1, "center_x_mm": 0, "center_y_mm": 0}
    )
    with pytest.raises(CadError) as failure:
        _solid(_compound([large, face]), "mixed")
    assert failure.value.code == "TYPE_MISMATCH"
    with pytest.raises(CadError) as failure:
        _solid(_compound([large, small.Reversed()]), "inverted")
    assert failure.value.code == "GEOMETRY_INVALID"


@pytest.mark.parametrize(
    "selector",
    [
        {"kind": [], "expected_count": 1},
        {"kind": "circle", "axis": [], "coordinate_mm": 0, "radius_mm": 1, "expected_count": 1},
        {"kind": "all_edges", "expected_count": True},
        {"kind": "line_parallel", "axis": [0, 0, 0], "expected_count": 1},
    ],
)
def test_malformed_selectors_raise_contract_errors_without_python_type_leaks(selector):
    graph = Graph()
    source, _, _ = graph.box()
    with pytest.raises(CadError):
        graph.add("solid.fillet@1", {"radius_mm": 1}, {"shape": source}, {"selector": selector})


@pytest.mark.parametrize(
    "depth,centers,removed",
    [
        (10, [[-5, 0], [5, 0]], 80 * math.pi),
        (4, [[0, 0]], 16 * math.pi),
    ],
)
def test_axial_hole_depth_and_multiple_centers_remove_expected_material(depth, centers, removed):
    graph = Graph()
    source, stock, _ = graph.box(width=20, height=20, depth=10)
    before = _brep_bytes(stock)
    _, changed, report = graph.add(
        "solid.hole@1",
        {"diameter_mm": 4, "depth_mm": depth, "start_z_mm": 0},
        {"base": source},
        {"centers_mm": centers},
    )
    assert _volume(changed) == pytest.approx(_volume(stock) - removed, rel=1e-10)
    assert _brep_bytes(stock) == before
    assert len(report["cuts"]) == len(centers)
    with pytest.raises(CadError) as failure:
        graph.add(
            "solid.hole@1",
            {"diameter_mm": 4, "depth_mm": 2, "start_z_mm": 0},
            {"base": source},
            {"centers_mm": [[100, 100]]},
        )
    assert failure.value.code == "HOLE_MISSES_BASE"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda f: f.update(extra=True),
        lambda f: f["parameters"].update(radius_mm=float("nan")),
        lambda f: f["parameters"].update(radius_mm=True),
        lambda f: f.update(data={"code": "arbitrary"}),
        lambda f: f["parameters"].update(radius_mm=-1),
    ],
)
def test_registry_rejects_unknown_members_nonfinite_and_code_payloads(mutation):
    feature = {
        "id": str(uuid4()),
        "name": "circle",
        "op": "profile.circle@1",
        "inputs": {},
        "parameters": {"radius_mm": 1, "center_x_mm": 0, "center_y_mm": 0},
    }
    feature = copy.deepcopy(feature)
    mutation(feature)
    with pytest.raises(CadError):
        validate_feature(feature, {})
