"""Real SolveSpace solves and OCCT profile topology; no solver or shape mocks."""

import copy
import math

import pytest
from deep3d_cad.contracts import CadError, PrecisionProfile
from deep3d_cad.sketch import build_sketch, validate_sketch
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.gp import gp_Vec
from OCP.GProp import GProp_GProps


def rectangle(width=20, height=10):
    return {
        "points": {"p0": [0, 0], "p1": [20, 0], "p2": [20, 10], "p3": [0, 10]},
        "lines": {
            "bottom": ["p0", "p1"],
            "right": ["p1", "p2"],
            "top": ["p2", "p3"],
            "left": ["p3", "p0"],
        },
        "circles": {},
        "arcs": {},
        "constraints": [
            {"id": "anchor", "kind": "fix", "point": "p0", "x_mm": 0, "y_mm": 0},
            {"id": "bottom_h", "kind": "horizontal", "line": "bottom"},
            {"id": "top_h", "kind": "horizontal", "line": "top"},
            {"id": "left_v", "kind": "vertical", "line": "left"},
            {"id": "right_v", "kind": "vertical", "line": "right"},
            {"id": "width", "kind": "distance", "a": "p0", "b": "p1", "value_mm": width},
            {"id": "height", "kind": "distance", "a": "p1", "b": "p2", "value_mm": height},
        ],
        "loops": [
            [{"entity": name, "reversed": False} for name in ("bottom", "right", "top", "left")]
        ],
    }


def circle_sketch(*, hole=False):
    sketch = {
        "points": {"center": [0, 0]},
        "lines": {},
        "arcs": {},
        "circles": {"outer": {"center": "center", "radius_mm": 8}},
        "constraints": [
            {"id": "anchor", "kind": "fix", "point": "center", "x_mm": 0, "y_mm": 0},
            {"id": "outer_diameter", "kind": "diameter", "circle": "outer", "value_mm": 20},
        ],
        "loops": [[{"entity": "outer", "reversed": False}]],
    }
    if hole:
        sketch["circles"]["inner"] = {"center": "center", "radius_mm": 4}
        sketch["constraints"].append(
            {"id": "inner_diameter", "kind": "diameter", "circle": "inner", "value_mm": 10}
        )
        sketch["loops"].append([{"entity": "inner", "reversed": False}])
    return sketch


def semicircle(*, ccw=True):
    return {
        "points": {"center": [0, 0], "start": [10, 0], "end": [-10, 0]},
        "lines": {"diameter_line": ["end", "start"]},
        "circles": {},
        "arcs": {"rim": {"center": "center", "start": "start", "end": "end", "ccw": ccw}},
        "constraints": [
            {"id": "fixed_center", "kind": "fix", "point": "center", "x_mm": 0, "y_mm": 0},
            {"id": "fixed_start", "kind": "fix", "point": "start", "x_mm": 10, "y_mm": 0},
            {"id": "diameter_h", "kind": "horizontal", "line": "diameter_line"},
        ],
        "loops": [
            [{"entity": "rim", "reversed": False}, {"entity": "diameter_line", "reversed": False}]
        ],
    }


def volume(face, height=3):
    builder = BRepPrimAPI_MakePrism(face, gp_Vec(0, 0, height), True, True)
    assert builder.IsDone()
    shape = builder.Shape()
    assert BRepCheck_Analyzer(shape).IsValid()
    properties = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, properties)
    return properties.Mass()


def test_constraint_edit_rebuilds_profile_with_stable_entity_ids():
    initial = rectangle()
    edited = copy.deepcopy(initial)
    edited["constraints"][-2]["value_mm"] = 30
    original_input = copy.deepcopy(initial)
    face, first = build_sketch(initial)
    changed, second = build_sketch(edited)
    assert first["solver"] == "SolveSpace"
    assert first["binding"] == "slvs" and first["version"] == "3.3"
    assert first["local_degrees_of_freedom"] == second["local_degrees_of_freedom"] == 0
    assert first["global_unique_solution_proven"] is False
    assert first["area_mm2"] == pytest.approx(200)
    assert second["area_mm2"] == pytest.approx(300)
    assert second["points_mm"]["p1"] == pytest.approx([30, 0])
    assert volume(face) == pytest.approx(600)
    assert volume(changed) == pytest.approx(900)
    assert set(first["points_mm"]) == set(second["points_mm"])
    assert initial == original_input


@pytest.mark.parametrize("hole", [False, True])
def test_circle_dimensions_and_inner_loops_are_real_profile_geometry(hole):
    face, report = build_sketch(circle_sketch(hole=hole))
    area = math.pi * (100 - (25 if hole else 0))
    assert report["radii_mm"]["outer"] == pytest.approx(10)
    assert report["area_mm2"] == pytest.approx(area)
    assert report["loop_count"] == (2 if hole else 1)
    assert volume(face) == pytest.approx(area * 3)


@pytest.mark.parametrize("ccw", [False, True])
def test_arc_direction_builds_correct_closed_half_circle(ccw):
    face, report = build_sketch(semicircle(ccw=ccw))
    assert report["local_degrees_of_freedom"] == 0
    assert report["area_mm2"] == pytest.approx(math.pi * 50)
    assert volume(face) == pytest.approx(math.pi * 150)


def test_underconstrained_sketch_reports_actual_remaining_dof():
    sketch = rectangle()
    sketch["constraints"].pop()
    with pytest.raises(CadError) as rejected:
        build_sketch(sketch)
    assert rejected.value.code == "SKETCH_UNDERCONSTRAINED"
    assert rejected.value.details["local_degrees_of_freedom"] == 1


@pytest.mark.parametrize(
    "other_width,code", [(30, "SKETCH_CONSTRAINT_CONFLICT"), (20, "SKETCH_REDUNDANT_CONSTRAINTS")]
)
def test_conflicting_and_redundant_constraints_are_not_silently_dropped(other_width, code):
    sketch = rectangle()
    sketch["constraints"].append(
        {"id": "second_width", "kind": "distance", "a": "p0", "b": "p1", "value_mm": other_width}
    )
    with pytest.raises(CadError) as rejected:
        build_sketch(sketch)
    assert rejected.value.code == code
    assert rejected.value.details["conflicting_constraints"]
    assert set(rejected.value.details["conflicting_constraints"]).issubset(
        {c["id"] for c in sketch["constraints"]}
    )


def test_repeated_calls_clear_native_solver_state():
    for width in (20, 40, 25):
        _, report = build_sketch(rectangle(width))
        assert report["area_mm2"] == pytest.approx(width * 10)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s["points"]["p0"].__setitem__(0, float("nan")),
        lambda s: s["points"]["p0"].__setitem__(0, True),
        lambda s: s["lines"]["bottom"].__setitem__(0, "missing"),
        lambda s: s["constraints"][0].__setitem__("point", "missing"),
        lambda s: s["constraints"][0].__setitem__("id", "bottom"),
        lambda s: s["constraints"][0].__setitem__("kind", "execute_script"),
        lambda s: s["constraints"][0].__setitem__("extra", 1),
        lambda s: s["loops"][0][0].__setitem__("entity", "missing"),
        lambda s: s["loops"][0][0].__setitem__("reversed", 1),
        lambda s: s.__setitem__("unknown", {}),
    ],
)
def test_invalid_inputs_rejected_before_native_solve(mutation):
    sketch = rectangle()
    mutation(sketch)
    with pytest.raises(CadError) as rejected:
        validate_sketch(sketch)
    assert rejected.value.code == "SKETCH_INVALID"


def test_ordered_loop_cannot_omit_a_boundary_edge():
    sketch = rectangle()
    sketch["loops"][0].pop()
    with pytest.raises(CadError, match="open|connected|closed"):
        build_sketch(sketch)


def test_hole_outside_outer_profile_rejected():
    sketch = circle_sketch(hole=True)
    sketch["points"]["outside_center"] = [30, 0]
    sketch["circles"]["inner"]["center"] = "outside_center"
    sketch["constraints"].append(
        {"id": "fixed_outside", "kind": "fix", "point": "outside_center", "x_mm": 30, "y_mm": 0}
    )
    with pytest.raises(CadError, match="outside"):
        build_sketch(sketch)


def test_holes_cannot_overlap_or_nest():
    sketch = circle_sketch(hole=True)
    sketch["circles"]["nested"] = {"center": "center", "radius_mm": 2}
    sketch["constraints"].append(
        {"id": "nested_diameter", "kind": "diameter", "circle": "nested", "value_mm": 4}
    )
    sketch["loops"].append([{"entity": "nested", "reversed": False}])
    with pytest.raises(CadError, match="overlap|nested"):
        build_sketch(sketch)


def test_precision_contract_required():
    with pytest.raises(CadError, match="precision"):
        build_sketch(rectangle(), {"working_tolerance_mm": 1e-5})
    assert build_sketch(rectangle(), PrecisionProfile())[1]["status"] == "pass"


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("parallel", [5, 0]),
        ("perpendicular", [0, 5]),
        ("angle", [3, 4]),
        ("equal_length", [0, 10]),
        ("midpoint", [5, 0]),
        ("point_on_line", [5, 0]),
        ("point_on_circle", [10, 0]),
        ("symmetric_h", [-10, 0]),
        ("symmetric_v", [10, 0]),
        ("coincident", [10, 0]),
    ],
)
def test_native_constraint_families_have_checked_solved_geometry(kind, expected):
    sketch = circle_sketch()
    sketch["points"].update({"reference_point": [10, 0], "moving": [4, 3]})
    sketch["lines"].update(
        {"reference_line": ["center", "reference_point"], "moving_line": ["center", "moving"]}
    )
    sketch["constraints"].append(
        {"id": "fix_reference", "kind": "fix", "point": "reference_point", "x_mm": 10, "y_mm": 0}
    )
    if kind in {"parallel", "perpendicular", "angle", "equal_length"}:
        constraint = {"id": "tested", "kind": kind, "a": "reference_line", "b": "moving_line"}
        if kind == "angle":
            constraint["value_rad"] = math.atan2(4, 3)
        sketch["constraints"].append(constraint)
        if kind == "equal_length":
            sketch["constraints"].append(
                {"id": "direction", "kind": "vertical", "line": "moving_line"}
            )
        else:
            sketch["constraints"].append(
                {
                    "id": "moving_distance",
                    "kind": "distance",
                    "a": "center",
                    "b": "moving",
                    "value_mm": 5,
                }
            )
    elif kind in {"midpoint", "point_on_line"}:
        sketch["constraints"].append(
            {"id": "tested", "kind": kind, "point": "moving", "line": "reference_line"}
        )
        if kind == "point_on_line":
            sketch["constraints"].append(
                {
                    "id": "moving_distance",
                    "kind": "distance",
                    "a": "center",
                    "b": "moving",
                    "value_mm": 5,
                }
            )
    elif kind == "point_on_circle":
        sketch["constraints"].extend(
            [
                {"id": "tested", "kind": kind, "point": "moving", "circle": "outer"},
                {"id": "direction", "kind": "horizontal", "line": "moving_line"},
            ]
        )
    else:
        sketch["constraints"].append(
            {"id": "tested", "kind": kind, "a": "reference_point", "b": "moving"}
        )
        if kind == "symmetric_h":
            sketch["points"]["moving"] = [-4, 3]
    _, report = build_sketch(sketch)
    assert report["points_mm"]["moving"] == pytest.approx(expected, abs=1e-7)
    assert all(check["residual"] <= check["tolerance"] for check in report["constraints"])


@pytest.mark.parametrize("ccw", [False, True])
def test_arc_line_tangency_uses_the_shared_endpoint_and_direction(ccw):
    sketch = circle_sketch()
    sketch["points"].update(
        {"arc_center": [30, 0], "arc_start": [40, 0], "contact": [30, 10], "line_end": [35, 11]}
    )
    sketch["arcs"]["construction_arc"] = {
        "center": "arc_center",
        "start": "arc_start",
        "end": "contact",
        "ccw": ccw,
    }
    sketch["lines"].update(
        {"radius_line": ["arc_center", "contact"], "tangent_line": ["contact", "line_end"]}
    )
    sketch["constraints"].extend(
        [
            {"id": "fix_arc_center", "kind": "fix", "point": "arc_center", "x_mm": 30, "y_mm": 0},
            {"id": "fix_arc_start", "kind": "fix", "point": "arc_start", "x_mm": 40, "y_mm": 0},
            {"id": "radius_direction", "kind": "vertical", "line": "radius_line"},
            {
                "id": "tangent_contact",
                "kind": "tangent",
                "a": "construction_arc",
                "b": "tangent_line",
            },
            {
                "id": "tangent_length",
                "kind": "distance",
                "a": "contact",
                "b": "line_end",
                "value_mm": 5,
            },
        ]
    )
    _, report = build_sketch(sketch)
    assert report["points_mm"]["contact"] == pytest.approx([30, 10])
    assert report["points_mm"]["line_end"] == pytest.approx([35, 10])


@pytest.mark.parametrize("ccw", [False, True])
def test_two_arc_tangency_solves_the_second_center(ccw):
    sketch = circle_sketch()
    sketch["points"].update(
        {
            "first_center": [30, 0],
            "first_start": [40, 0],
            "contact": [30, 10],
            "second_center": [31, 20],
            "second_end": [40, 20],
        }
    )
    sketch["arcs"].update(
        {
            "first_arc": {
                "center": "first_center",
                "start": "first_start",
                "end": "contact",
                "ccw": ccw,
            },
            "second_arc": {
                "center": "second_center",
                "start": "contact",
                "end": "second_end",
                "ccw": ccw,
            },
        }
    )
    sketch["lines"]["radius_line"] = ["first_center", "contact"]
    sketch["constraints"].extend(
        [
            {
                "id": "fix_first_center",
                "kind": "fix",
                "point": "first_center",
                "x_mm": 30,
                "y_mm": 0,
            },
            {"id": "fix_first_start", "kind": "fix", "point": "first_start", "x_mm": 40, "y_mm": 0},
            {"id": "radius_direction", "kind": "vertical", "line": "radius_line"},
            {"id": "fix_second_end", "kind": "fix", "point": "second_end", "x_mm": 40, "y_mm": 20},
            {"id": "tangent_contact", "kind": "tangent", "a": "first_arc", "b": "second_arc"},
        ]
    )
    _, report = build_sketch(sketch)
    assert report["points_mm"]["second_center"] == pytest.approx([30, 20])


def test_self_intersecting_closed_loop_does_not_become_a_face():
    points = {
        f"point{i}": [10 * math.cos(2 * math.pi * i / 5), 10 * math.sin(2 * math.pi * i / 5)]
        for i in range(5)
    }
    order = [0, 2, 4, 1, 3]
    sketch = {
        "points": points,
        "lines": {f"edge{i}": [f"point{order[i]}", f"point{order[(i + 1) % 5]}"] for i in range(5)},
        "circles": {},
        "arcs": {},
        "constraints": [
            {"id": f"fixed{i}", "kind": "fix", "point": name, "x_mm": xy[0], "y_mm": xy[1]}
            for i, (name, xy) in enumerate(points.items())
        ],
        "loops": [[{"entity": f"edge{i}", "reversed": False} for i in range(5)]],
    }
    with pytest.raises(CadError, match="valid planar face|loop area|degenerate"):
        build_sketch(sketch)


def test_a_hole_touching_the_outer_loop_is_rejected():
    sketch = circle_sketch(hole=True)
    sketch["points"]["hole_center"] = [5, 0]
    sketch["circles"]["inner"]["center"] = "hole_center"
    sketch["constraints"].append(
        {"id": "fixed_hole", "kind": "fix", "point": "hole_center", "x_mm": 5, "y_mm": 0}
    )
    with pytest.raises(CadError, match="touch|cross"):
        build_sketch(sketch)
