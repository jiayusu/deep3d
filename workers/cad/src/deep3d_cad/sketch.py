"""Bounded, persistent 2D sketches solved by SolveSpace and built as OCCT faces.

The public input is plain geometry/constraint data. Solver handles exist only for
one call, and local zero DoF is not a proof of global nonlinear uniqueness.
"""

from __future__ import annotations

import math
import re
import threading
from importlib.metadata import version
from typing import Any

from .contracts import CadError, PrecisionProfile

_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_LOCK = threading.Lock()
_LENGTH_TOL = 1e-7
_ANGLE_TOL = 1e-8
_FIELDS = {"points", "lines", "circles", "arcs", "constraints", "loops"}
_CONSTRAINTS = {
    "fix": {"point", "x_mm", "y_mm"},
    "horizontal": {"line"},
    "vertical": {"line"},
    "distance": {"a", "b", "value_mm"},
    "coincident": {"a", "b"},
    "parallel": {"a", "b"},
    "perpendicular": {"a", "b"},
    "equal_length": {"a", "b"},
    "angle": {"a", "b", "value_rad"},
    "diameter": {"circle", "value_mm"},
    "point_on_line": {"point", "line"},
    "point_on_circle": {"point", "circle"},
    "tangent": {"a", "b"},
    "midpoint": {"point", "line"},
    "symmetric_h": {"a", "b"},
    "symmetric_v": {"a", "b"},
}


def _invalid(message: str, **details: Any) -> CadError:
    return CadError("SKETCH_INVALID", message, details)


def _object(value: Any, fields: set[str] | None, label: str) -> dict:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise _invalid(f"{label} must be an object")
    if fields is not None and set(value) != fields:
        raise _invalid(f"{label} fields differ from the sketch contract")
    return value


def _number(value: Any, label: str, lower: float, upper: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(f"{label} must be finite numeric data")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise _invalid(f"{label} is outside finite f64") from exc
    if not math.isfinite(number) or not lower <= number <= upper:
        raise _invalid(f"{label} is outside its supported range")
    return number


def _reference(value: Any, names: dict, label: str) -> str:
    if not isinstance(value, str) or value not in names:
        raise _invalid(f"{label} refers to an unknown entity")
    return value


def _arc_endpoints(sketch: dict, name: str) -> tuple[str, str]:
    arc = sketch["arcs"][name]
    return (arc["start"], arc["end"]) if arc["ccw"] else (arc["end"], arc["start"])


def _tangent_contact(sketch: dict, a: str, b: str) -> tuple[str, str | None, int, int]:
    arcs, lines = sketch["arcs"], sketch["lines"]
    if a in lines and b in arcs:
        a, b = b, a
    if a not in arcs or b not in arcs | lines:
        raise _invalid("Tangency requires an arc and a line, or two arcs")
    endpoints_a = _arc_endpoints(sketch, a)
    endpoints_b = _arc_endpoints(sketch, b) if b in arcs else tuple(lines[b])
    shared = set(endpoints_a) & set(endpoints_b)
    if len(shared) != 1:
        raise _invalid("Tangent curves require exactly one shared endpoint identity")
    point = shared.pop()
    return a, b, int(endpoints_a[1] == point), int(endpoints_b[1] == point)


def validate_sketch(sketch: Any) -> None:
    """Validate every reference and primitive before entering the native solver."""
    sketch = _object(sketch, _FIELDS, "Sketch")
    points = _object(sketch["points"], None, "Sketch.points")
    lines = _object(sketch["lines"], None, "Sketch.lines")
    circles = _object(sketch["circles"], None, "Sketch.circles")
    arcs = _object(sketch["arcs"], None, "Sketch.arcs")
    if not 1 <= len(points) <= 64 or not 1 <= len(lines) + len(circles) + len(arcs) <= 128:
        raise _invalid("Sketch requires 1..64 points and 1..128 curves")
    constraints = sketch["constraints"]
    if not isinstance(constraints, list) or len(constraints) > 256:
        raise _invalid("Sketch supports at most 256 explicit constraints")
    identities = set()
    for mapping in (points, lines, circles, arcs):
        for name in mapping:
            if not _NAME.fullmatch(name) or name in identities:
                raise _invalid("Sketch entity identities must be unique bounded ASCII names")
            identities.add(name)
    for name, point in points.items():
        if not isinstance(point, list) or len(point) != 2:
            raise _invalid("Point initial coordinates require two lengths", point=name)
        for coordinate in point:
            _number(coordinate, "Point coordinate", -1000, 1000)
    for name, line in lines.items():
        if not isinstance(line, list) or len(line) != 2:
            raise _invalid("A line requires two point references", line=name)
        for point in line:
            _reference(point, points, "Line endpoint")
        if line[0] == line[1] or math.dist(points[line[0]], points[line[1]]) <= 1e-6:
            raise _invalid("A line needs distinct, nondegenerate seed endpoints", line=name)
    for name, raw in circles.items():
        circle = _object(raw, {"center", "radius_mm"}, "Circle")
        _reference(circle["center"], points, "Circle center")
        _number(circle["radius_mm"], "Circle seed radius", 0.05, 500)
    for name, raw in arcs.items():
        arc = _object(raw, {"center", "start", "end", "ccw"}, "Arc")
        for key in ("center", "start", "end"):
            _reference(arc[key], points, "Arc point")
        if len({arc[key] for key in ("center", "start", "end")}) != 3:
            raise _invalid("An arc needs distinct center/start/end identities", arc=name)
        if type(arc["ccw"]) is not bool:
            raise _invalid("Arc ccw must be boolean")
        for key in ("start", "end"):
            radius = math.dist(points[arc["center"]], points[arc[key]])
            _number(radius, "Arc seed radius", 0.05, 500)
        if math.dist(points[arc["start"]], points[arc["end"]]) <= 1e-6:
            raise _invalid("Use a circle for a full circular loop", arc=name)

    for raw in constraints:
        constraint = _object(raw, None, "Constraint")
        kind = constraint.get("kind")
        if not isinstance(kind, str) or kind not in _CONSTRAINTS:
            raise _invalid("Unsupported sketch constraint")
        if set(constraint) != {"id", "kind"} | _CONSTRAINTS[kind]:
            raise _invalid("Constraint fields differ from its operation")
        identifier = constraint["id"]
        if (
            not isinstance(identifier, str)
            or not _NAME.fullmatch(identifier)
            or identifier in identities
        ):
            raise _invalid("Constraint identities must be unique bounded names")
        identities.add(identifier)
        if kind == "fix":
            _reference(constraint["point"], points, "Fixed point")
            _number(constraint["x_mm"], "Fixed x", -1000, 1000)
            _number(constraint["y_mm"], "Fixed y", -1000, 1000)
        elif kind in {"horizontal", "vertical"}:
            _reference(constraint["line"], lines, "Constraint line")
        elif kind in {"distance", "coincident", "symmetric_h", "symmetric_v"}:
            _reference(constraint["a"], points, "Constraint point a")
            _reference(constraint["b"], points, "Constraint point b")
            if constraint["a"] == constraint["b"]:
                raise _invalid("Binary point constraints require distinct point identities")
            if kind == "distance":
                _number(constraint["value_mm"], "Point distance", 1e-5, 2000)
        elif kind in {"parallel", "perpendicular", "equal_length", "angle"}:
            _reference(constraint["a"], lines, "Constraint line a")
            _reference(constraint["b"], lines, "Constraint line b")
            if constraint["a"] == constraint["b"]:
                raise _invalid("Binary line constraints require distinct lines")
            if kind == "angle":
                angle = _number(constraint["value_rad"], "Unsigned line angle", 0, math.pi)
                if angle in (0, math.pi):
                    raise _invalid("Use parallel for zero or pi line angles")
        elif kind == "diameter":
            _reference(constraint["circle"], circles, "Dimensioned circle")
            _number(constraint["value_mm"], "Circle diameter", 0.1, 1000)
        elif kind in {"point_on_line", "midpoint"}:
            _reference(constraint["point"], points, "Constraint point")
            _reference(constraint["line"], lines, "Constraint line")
        elif kind == "point_on_circle":
            _reference(constraint["point"], points, "Constraint point")
            _reference(constraint["circle"], circles, "Constraint circle")
        elif kind == "tangent":
            _reference(constraint["a"], arcs | lines, "Tangent curve a")
            _reference(constraint["b"], arcs | lines, "Tangent curve b")
            _tangent_contact(sketch, constraint["a"], constraint["b"])

    loops = sketch["loops"]
    if not isinstance(loops, list) or not 1 <= len(loops) <= 32:
        raise _invalid("Sketch needs one outer loop and at most 31 hole loops")
    used = set()
    for loop in loops:
        if not isinstance(loop, list) or not 1 <= len(loop) <= 128:
            raise _invalid("A loop needs 1..128 explicitly ordered edges")
        for raw in loop:
            edge = _object(raw, {"entity", "reversed"}, "Loop edge")
            name = _reference(edge["entity"], lines | circles | arcs, "Loop curve")
            if name in used or type(edge["reversed"]) is not bool:
                raise _invalid("A curve can appear only once in the profile loops")
            if name in circles and len(loop) != 1:
                raise _invalid("A complete circle must form its own loop")
            used.add(name)


def _solve(sketch: dict) -> tuple[dict, dict, dict]:
    import slvs

    if version("slvs").removeprefix("v") != "3.3":
        raise CadError("SKETCH_SOLVER_UNSUPPORTED", "Expected the pinned slvs 3.3 binding")
    with _LOCK:
        slvs.clear_sketch()
        try:
            quaternion = slvs.make_quaternion(1, 0, 0, 0, 1, 0)
            normal = slvs.add_normal_3d(1, *quaternion)
            origin = slvs.add_point_3d(1, 0, 0, 0)
            workplane = slvs.add_workplane(1, origin, normal)
            points = {
                name: slvs.add_point_2d(2, *xy, workplane)
                for name, xy in sorted(sketch["points"].items())
            }
            curves = {
                name: slvs.add_line_2d(2, points[a], points[b], workplane)
                for name, (a, b) in sorted(sketch["lines"].items())
            }
            radii = {}
            for name, circle in sorted(sketch["circles"].items()):
                radii[name] = slvs.add_distance(2, circle["radius_mm"], workplane)
                curves[name] = slvs.add_circle(
                    2, normal, points[circle["center"]], radii[name], workplane
                )
            for name, arc in sorted(sketch["arcs"].items()):
                start, end = _arc_endpoints(sketch, name)
                curves[name] = slvs.add_arc(
                    2, normal, points[arc["center"]], points[start], points[end], workplane
                )
            handles = {}
            for constraint in sketch["constraints"]:
                kind = constraint["kind"]
                if kind == "fix":
                    fixed = slvs.add_point_2d(1, constraint["x_mm"], constraint["y_mm"], workplane)
                    native = slvs.coincident(2, points[constraint["point"]], fixed, workplane)
                elif kind in {"horizontal", "vertical"}:
                    native = getattr(slvs, kind)(2, curves[constraint["line"]], workplane)
                elif kind in {"distance", "coincident", "symmetric_h", "symmetric_v"}:
                    a, b = points[constraint["a"]], points[constraint["b"]]
                    native = (
                        slvs.distance(2, a, b, constraint["value_mm"], workplane)
                        if kind == "distance"
                        else getattr(slvs, kind)(2, a, b, workplane)
                    )
                elif kind in {"parallel", "perpendicular", "equal_length", "angle"}:
                    a, b = curves[constraint["a"]], curves[constraint["b"]]
                    if kind == "angle":
                        native = slvs.angle(
                            2, a, b, math.degrees(constraint["value_rad"]), workplane
                        )
                    else:
                        native = getattr(slvs, "equal" if kind == "equal_length" else kind)(
                            2, a, b, workplane
                        )
                elif kind == "diameter":
                    native = slvs.diameter(2, curves[constraint["circle"]], constraint["value_mm"])
                elif kind in {"point_on_line", "point_on_circle"}:
                    target = "line" if kind == "point_on_line" else "circle"
                    symbol = "PT_ON_LINE" if kind == "point_on_line" else "PT_ON_CIRCLE"
                    native = slvs.add_constraint(
                        2,
                        getattr(slvs.ConstraintType, symbol),
                        workplane,
                        0,
                        points[constraint["point"]],
                        slvs.E_NONE,
                        curves[constraint[target]],
                    )
                elif kind == "midpoint":
                    native = slvs.midpoint(
                        2, points[constraint["point"]], curves[constraint["line"]], workplane
                    )
                else:
                    a, b, end_a, end_b = _tangent_contact(sketch, constraint["a"], constraint["b"])
                    symbol = "ARC_LINE_TANGENT" if b in sketch["lines"] else "CURVE_CURVE_TANGENT"
                    native = slvs.add_constraint(
                        2,
                        getattr(slvs.ConstraintType, symbol),
                        workplane,
                        0,
                        slvs.E_NONE,
                        slvs.E_NONE,
                        curves[a],
                        curves[b],
                        slvs.E_NONE,
                        slvs.E_NONE,
                        end_a,
                        end_b,
                    )
                handles[int(native["h"])] = constraint["id"]

            raw = slvs.solve_sketch(2, True)
            if not isinstance(raw, tuple) or len(raw) != 2 or not isinstance(raw[0], dict):
                raise CadError("SKETCH_SOLVER_PROTOCOL", "Unexpected solver result contract")
            status, bad = raw
            code, dof = int(status["result"]), int(status["dof"])
            diagnostic = {
                "solver": "SolveSpace",
                "binding": "slvs",
                "version": "3.3",
                "result_code": code,
                "local_degrees_of_freedom": dof,
                "global_unique_solution_proven": False,
                "conflicting_constraints": [
                    handles.get(int(handle), f"native-{handle}") for handle in bad
                ],
            }
            if code != int(slvs.ResultFlag.OKAY):
                error = (
                    "SKETCH_REDUNDANT_CONSTRAINTS"
                    if code == int(slvs.ResultFlag.REDUNDANT_OKAY)
                    else "SKETCH_CONSTRAINT_CONFLICT"
                    if code == int(slvs.ResultFlag.INCONSISTENT)
                    else "SKETCH_SOLVER_FAILED"
                )
                raise CadError(
                    error, "Sketch constraints did not yield an accepted solve", diagnostic
                )
            if dof != 0:
                raise CadError(
                    "SKETCH_UNDERCONSTRAINED", "Sketch retains free variables", diagnostic
                )
            solved_points = {
                name: [slvs.get_param_value(handle) for handle in entity["param"][:2]]
                for name, entity in points.items()
            }
            solved_radii = {
                name: slvs.get_param_value(entity["param"][0]) for name, entity in radii.items()
            }
            return solved_points, solved_radii, diagnostic
        finally:
            slvs.clear_sketch()


def _residuals(sketch: dict, points: dict, radii: dict) -> list[dict]:
    def vector(name: str) -> tuple[float, float]:
        a, b = (points[p] for p in sketch["lines"][name])
        return b[0] - a[0], b[1] - a[1]

    def length(name: str) -> float:
        return math.hypot(*vector(name))

    result = []
    for constraint in sketch["constraints"]:
        kind, tolerance = constraint["kind"], _LENGTH_TOL
        if kind == "fix":
            error = math.dist(points[constraint["point"]], [constraint["x_mm"], constraint["y_mm"]])
        elif kind in {"horizontal", "vertical"}:
            error = abs(vector(constraint["line"])[1 if kind == "horizontal" else 0])
        elif kind in {"distance", "coincident"}:
            error = abs(
                math.dist(points[constraint["a"]], points[constraint["b"]])
                - (constraint["value_mm"] if kind == "distance" else 0)
            )
        elif kind in {"parallel", "perpendicular", "angle"}:
            a, b = vector(constraint["a"]), vector(constraint["b"])
            scale = length(constraint["a"]) * length(constraint["b"])
            if scale <= 1e-12:
                raise _invalid("Solved line collapsed")
            cosine = (a[0] * b[0] + a[1] * b[1]) / scale
            error = (
                abs(a[0] * b[1] - a[1] * b[0]) / scale
                if kind == "parallel"
                else abs(cosine)
                if kind == "perpendicular"
                else abs(cosine - math.cos(constraint["value_rad"]))
            )
            tolerance = _ANGLE_TOL
        elif kind == "equal_length":
            error = abs(length(constraint["a"]) - length(constraint["b"]))
        elif kind == "diameter":
            error = abs(2 * radii[constraint["circle"]] - constraint["value_mm"])
        elif kind == "point_on_line":
            a = points[sketch["lines"][constraint["line"]][0]]
            p, v = points[constraint["point"]], vector(constraint["line"])
            error = abs((p[0] - a[0]) * v[1] - (p[1] - a[1]) * v[0]) / length(constraint["line"])
        elif kind == "point_on_circle":
            circle = sketch["circles"][constraint["circle"]]
            error = abs(
                math.dist(points[constraint["point"]], points[circle["center"]])
                - radii[constraint["circle"]]
            )
        elif kind == "midpoint":
            a, b = (points[p] for p in sketch["lines"][constraint["line"]])
            error = math.dist(points[constraint["point"]], [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2])
        elif kind in {"symmetric_h", "symmetric_v"}:
            a, b = points[constraint["a"]], points[constraint["b"]]
            error = (
                math.hypot(a[0] + b[0], a[1] - b[1])
                if kind == "symmetric_h"
                else math.hypot(a[0] - b[0], a[1] + b[1])
            )
        else:
            a, b, end_a, end_b = _tangent_contact(sketch, constraint["a"], constraint["b"])
            arc_a = sketch["arcs"][a]
            p = points[_arc_endpoints(sketch, a)[end_a]]
            c = points[arc_a["center"]]
            v = p[0] - c[0], p[1] - c[1]
            if b in sketch["lines"]:
                w = vector(b)
                error = abs(v[0] * w[0] + v[1] * w[1]) / (math.hypot(*v) * math.hypot(*w))
            else:
                q = points[_arc_endpoints(sketch, b)[end_b]]
                center = points[sketch["arcs"][b]["center"]]
                w = q[0] - center[0], q[1] - center[1]
                error = abs(v[0] * w[1] - v[1] * w[0]) / (math.hypot(*v) * math.hypot(*w))
            tolerance = _ANGLE_TOL
        if not math.isfinite(error) or error > tolerance:
            raise CadError(
                "SKETCH_RESIDUAL_FAILED",
                "Solved data fails the independent constraint check",
                {"constraint_id": constraint["id"], "residual": error, "tolerance": tolerance},
            )
        result.append({"id": constraint["id"], "residual": error, "tolerance": tolerance})
    return result


def _mass(shape) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    properties = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, properties)
    return properties.Mass()


def _wire(sketch: dict, loop: list, points: dict, radii: dict, tolerance: float):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire
    from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pnt

    builder = BRepBuilderAPI_MakeWire()
    connections, signed_area = [], 0.0
    for reference in loop:
        name, reverse = reference["entity"], reference["reversed"]
        if name in sketch["lines"]:
            start, end = (points[p] for p in sketch["lines"][name])
            if math.dist(start, end) <= _LENGTH_TOL:
                raise _invalid("Solved line collapsed", entity=name)
            edge = BRepBuilderAPI_MakeEdge(gp_Pnt(*start, 0), gp_Pnt(*end, 0)).Edge()
            area = (start[0] * end[1] - end[0] * start[1]) / 2
        elif name in sketch["circles"]:
            center, radius = points[sketch["circles"][name]["center"]], radii[name]
            circle = gp_Circ(gp_Ax2(gp_Pnt(*center, 0), gp_Dir(0, 0, 1)), radius)
            edge = BRepBuilderAPI_MakeEdge(circle).Edge()
            start = end = [center[0] + radius, center[1]]
            area = math.pi * radius * radius
        else:
            arc = sketch["arcs"][name]
            center, start, end = points[arc["center"]], points[arc["start"]], points[arc["end"]]
            radius = math.dist(center, start)
            if abs(radius - math.dist(center, end)) > _LENGTH_TOL:
                raise _invalid("Solved arc has inconsistent radii", entity=name)
            if not 0.05 <= radius <= 500 or math.dist(start, end) <= _LENGTH_TOL:
                raise _invalid("Solved arc is degenerate or outside the radius domain", entity=name)
            a0, a1 = (math.atan2(p[1] - center[1], p[0] - center[0]) for p in (start, end))
            sweep = ((a1 - a0) % math.tau) if arc["ccw"] else -((a0 - a1) % math.tau)
            circle = gp_Circ(gp_Ax2(gp_Pnt(*center, 0), gp_Dir(0, 0, 1)), radius)
            edge = (
                BRepBuilderAPI_MakeEdge(circle, a0, a0 + sweep).Edge()
                if arc["ccw"]
                else BRepBuilderAPI_MakeEdge(circle, a1, a1 - sweep).Edge()
            )
            if not arc["ccw"]:
                edge.Reverse()
            area = (
                center[0] * (end[1] - start[1])
                - center[1] * (end[0] - start[0])
                + radius * radius * sweep
            ) / 2
        if reverse:
            edge.Reverse()
            start, end, area = end, start, -area
        connections.append((start, end))
        signed_area += area
        builder.Add(edge)
        if not builder.IsDone():
            raise _invalid("Sketch edges cannot form a connected wire")
    for index, (_, end) in enumerate(connections):
        if math.dist(end, connections[(index + 1) % len(connections)][0]) > tolerance:
            raise _invalid("Sketch loop is open or incorrectly ordered")
    wire = builder.Wire()
    if not wire.Closed() or abs(signed_area) <= tolerance * tolerance:
        raise _invalid("Sketch loop is degenerate or not closed")
    return wire, signed_area


def _face(sketch: dict, points: dict, radii: dict, precision: PrecisionProfile):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.gp import gp_Dir, gp_Pln, gp_Pnt
    from OCP.TopoDS import TopoDS

    plane = gp_Pln(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))
    wires, areas, faces = [], [], []
    for index, loop in enumerate(sketch["loops"]):
        wire, signed = _wire(sketch, loop, points, radii, precision.working_tolerance_mm)
        if signed < 0:
            wire = TopoDS.Wire(wire.Reversed())
        single = BRepBuilderAPI_MakeFace(plane, wire, True)
        if not single.IsDone() or not BRepCheck_Analyzer(single.Face()).IsValid():
            raise _invalid("Sketch loop does not bound a valid planar face")
        area = _mass(single.Face())
        if abs(area - abs(signed)) > max(1e-8, abs(signed) * 1e-10):
            raise _invalid("Sketch loop area differs from its analytic boundary")
        faces.append(single.Face())
        areas.append(area)
        wires.append(wire if index == 0 else TopoDS.Wire(wire.Reversed()))
    for index in range(1, len(wires)):
        difference = BRepAlgoAPI_Cut(faces[index], faces[0])
        if not difference.IsDone() or abs(_mass(difference.Shape())) > 1e-8:
            raise _invalid("A hole loop lies outside the outer profile")
        for previous in range(index):
            distance = BRepExtrema_DistShapeShape(wires[index], wires[previous])
            distance.Perform()
            if not distance.IsDone() or distance.Value() <= precision.working_tolerance_mm:
                raise _invalid("Sketch loops touch or cross")
            if previous > 0:
                common = BRepAlgoAPI_Common(faces[index], faces[previous])
                if not common.IsDone() or abs(_mass(common.Shape())) > 1e-8:
                    raise _invalid("Hole loops overlap or are nested")
    builder = BRepBuilderAPI_MakeFace(plane, wires[0], True)
    for wire in wires[1:]:
        builder.Add(wire)
    face = builder.Face()
    expected = areas[0] - sum(areas[1:])
    if (
        expected <= precision.working_tolerance_mm**2
        or not BRepCheck_Analyzer(face).IsValid()
        or abs(_mass(face) - expected) > max(1e-8, expected * 1e-10)
    ):
        raise _invalid("Sketch does not yield a valid outer region with disjoint holes")
    return face, expected


def build_sketch(sketch: Any, precision: PrecisionProfile | None = None):
    """Solve a fully constrained sketch and produce one bounded XY planar face."""
    validate_sketch(sketch)
    precision = precision or PrecisionProfile()
    if not isinstance(precision, PrecisionProfile):
        raise _invalid("Expected the versioned precision profile")
    points, radii, diagnostic = _solve(sketch)
    for point in points.values():
        for coordinate in point:
            _number(coordinate, "Solved point coordinate", -1000, 1000)
    for radius in radii.values():
        _number(radius, "Solved circle radius", 0.05, 500)
    for name, (a, b) in sketch["lines"].items():
        if math.dist(points[a], points[b]) <= _LENGTH_TOL:
            raise _invalid("Solved line collapsed", entity=name)
    for name, arc in sketch["arcs"].items():
        start_radius = math.dist(points[arc["center"]], points[arc["start"]])
        end_radius = math.dist(points[arc["center"]], points[arc["end"]])
        if (
            not 0.05 <= start_radius <= 500
            or abs(start_radius - end_radius) > _LENGTH_TOL
            or math.dist(points[arc["start"]], points[arc["end"]]) <= _LENGTH_TOL
        ):
            raise _invalid("Solved arc is degenerate or inconsistent", entity=name)
    residuals = _residuals(sketch, points, radii)
    face, area = _face(sketch, points, radii, precision)
    diagnostic.update(
        status="pass",
        points_mm=points,
        radii_mm=radii,
        area_mm2=area,
        constraints=residuals,
        loop_count=len(sketch["loops"]),
    )
    return face, diagnostic
