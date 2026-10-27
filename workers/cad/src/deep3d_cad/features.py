"""Typed mechanical feature registry for direct, non-scripted OCCT execution.

Edge references are geometric predicates resolved against each input shape. Local
topology indices never become persistent references. All outputs are new shapes.
"""

from __future__ import annotations

import math
import unicodedata
from typing import Any
from uuid import UUID

from .contracts import CadError, PrecisionProfile

_TAU = math.tau
_AXIS_TOL = 1e-9
_FEATURE_FIELDS = {"id", "name", "op", "inputs", "parameters"}
_AXES = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}
_ORIGIN = {"origin_x_mm", "origin_y_mm", "origin_z_mm"}
_AXIS = {"axis_x", "axis_y", "axis_z"}

# Inputs, numeric parameters, structured data members, output category.
REGISTRY = {
    "profile.circle@1": ({}, {"radius_mm", "center_x_mm", "center_y_mm"}, set(), "profile"),
    "profile.rectangle@1": (
        {},
        {"width_mm", "height_mm", "center_x_mm", "center_y_mm"},
        set(),
        "profile",
    ),
    "profile.polygon@1": ({}, set(), {"points_mm"}, "profile"),
    "solid.extrude@1": ({"profile": "profile"}, {"distance_mm"}, set(), "solid"),
    "solid.revolve@1": ({"profile": "profile"}, {"angle_rad"} | _ORIGIN | _AXIS, set(), "solid"),
    "solid.cut@1": ({"base": "solid", "tool": "solid"}, set(), set(), "solid"),
    "solid.fuse@1": ({"base": "solid", "tool": "solid"}, set(), set(), "solid"),
    "solid.common@1": ({"base": "solid", "tool": "solid"}, set(), set(), "solid"),
    "shape.transform@1": (
        {"shape": "solid"},
        {"dx_mm", "dy_mm", "dz_mm", "angle_rad"} | _AXIS,
        set(),
        "solid",
    ),
    "solid.fillet@1": ({"shape": "solid"}, {"radius_mm"}, {"selector"}, "solid"),
    "solid.chamfer@1": ({"shape": "solid"}, {"distance_mm"}, {"selector"}, "solid"),
    "shape.linear_pattern@1": (
        {"shape": "solid"},
        {"step_x_mm", "step_y_mm", "step_z_mm"},
        {"count", "fuse"},
        "solid",
    ),
    "shape.circular_pattern@1": (
        {"shape": "solid"},
        {"step_angle_rad"} | _ORIGIN | _AXIS,
        {"count", "fuse"},
        "solid",
    ),
    "solid.hole@1": (
        {"base": "solid"},
        {"diameter_mm", "depth_mm", "start_z_mm"},
        {"centers_mm"},
        "solid",
    ),
    "assembly.compound@1": (None, set(), set(), "solid"),
}


def _error(code: str, message: str, **details: Any) -> CadError:
    return CadError(code, message, details)


def _object(value: Any, fields: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise _error("SCHEMA_INVALID", f"{label} fields differ from the operation contract")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error("PARAMETER_INVALID", f"{label} must be a finite number")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise _error("PARAMETER_INVALID", f"{label} exceeds finite f64") from exc
    if not math.isfinite(result):
        raise _error("PARAMETER_INVALID", f"{label} must be finite")
    return result


def _uuid(value: Any) -> None:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value or UUID(value).int == 0:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise _error(
            "SCHEMA_INVALID", "Feature references require canonical nonzero UUIDs"
        ) from exc


def _vector(value: Any, label: str, *, unit: bool = False) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise _error("PARAMETER_INVALID", f"{label} needs three coordinates")
    result = [_number(v, label) for v in value]
    if unit:
        if abs(math.sqrt(sum(v * v for v in result)) - 1) > _AXIS_TOL:
            raise _error("PARAMETER_INVALID", f"{label} must be a unit direction")
    elif any(abs(v) > 1000 for v in result):
        raise _error("OUTSIDE_QUALIFIED_ENVELOPE", f"{label} coordinates exceed 1000 mm")
    return result


def _axis(parameters: dict) -> list[float]:
    return _vector([parameters[f"axis_{axis}"] for axis in "xyz"], "axis", unit=True)


def _positive(value: float, label: str, *, lower: float = 0.0001, upper: float = 1000) -> None:
    if not lower <= value <= upper:
        raise _error("OUTSIDE_QUALIFIED_ENVELOPE", f"{label} requires {lower}..{upper} mm")


def _points(value: Any, label: str, minimum: int, maximum: int) -> list[list[float]]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise _error("PARAMETER_INVALID", f"{label} needs {minimum}..{maximum} XY points")
    result = []
    for point in value:
        if not isinstance(point, list) or len(point) != 2:
            raise _error("PARAMETER_INVALID", f"{label} entries need X and Y")
        numbers = [_number(v, label) for v in point]
        if any(abs(v) > 1000 for v in numbers):
            raise _error("OUTSIDE_QUALIFIED_ENVELOPE", f"{label} exceeds 1000 mm")
        result.append(numbers)
    if len({tuple(p) for p in result}) != len(result):
        raise _error("PARAMETER_INVALID", f"{label} points must be distinct")
    return result


def _polygon(points: list[list[float]]) -> None:
    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    area = sum(a[0] * b[1] - a[1] * b[0] for a, b in zip(points, points[1:] + points[:1])) / 2
    if abs(area) < 1e-8:
        raise _error("PROFILE_DEGENERATE", "Polygon has zero or negligible signed area")
    for i, a in enumerate(points):
        b = points[(i + 1) % len(points)]
        if math.dist(a, b) < 0.0001:
            raise _error("PROFILE_DEGENERATE", "Polygon edge is shorter than 0.0001 mm")
        for j in range(i + 1, len(points)):
            if j == i + 1 or i == 0 and j == len(points) - 1:
                continue
            c, d = points[j], points[(j + 1) % len(points)]
            orientations = [cross(a, b, c), cross(a, b, d), cross(c, d, a), cross(c, d, b)]
            # Nonadjacent touching and overlapping edges are ambiguous profiles too.
            if (
                max(min(a[0], b[0]), min(c[0], d[0]))
                <= min(max(a[0], b[0]), max(c[0], d[0])) + 1e-10
                and max(min(a[1], b[1]), min(c[1], d[1]))
                <= min(max(a[1], b[1]), max(c[1], d[1])) + 1e-10
                and orientations[0] * orientations[1] <= 1e-20
                and orientations[2] * orientations[3] <= 1e-20
            ):
                raise _error("PROFILE_SELF_INTERSECTION", "Polygon edges cross or touch")


def _selector(value: Any) -> dict:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("kind"), str)
        or value["kind"]
        not in {
            "all_edges",
            "line_parallel",
            "plane",
            "circle",
        }
    ):
        raise _error("SELECTOR_INVALID", "Unsupported semantic edge selector")
    kind = value["kind"]
    fields = {"kind", "expected_count"}
    if kind == "line_parallel":
        fields |= {"axis"}
    elif kind == "plane":
        fields |= {"axis", "coordinate_mm"}
    elif kind == "circle":
        fields |= {"radius_mm", "axis", "coordinate_mm"}
    _object(value, fields, "EdgeSelector")
    if type(value["expected_count"]) is not int or not 1 <= value["expected_count"] <= 4096:
        raise _error("SELECTOR_INVALID", "expected_count requires 1..4096 edges")
    if kind == "line_parallel":
        _vector(value["axis"], "selector axis", unit=True)
    elif kind in {"plane", "circle"}:
        if not isinstance(value["axis"], str) or value["axis"] not in _AXES:
            raise _error("SELECTOR_INVALID", "Plane/circle axis must be x, y or z")
        if abs(_number(value["coordinate_mm"], "selector coordinate")) > 1000:
            raise _error("SELECTOR_INVALID", "Selector plane exceeds 1000 mm")
        if kind == "circle":
            _positive(_number(value["radius_mm"], "selector radius"), "selector radius")
    return value


def validate_feature(feature: dict, seen_types: dict[str, str]) -> str:
    """Reject malformed/unsupported features before an output directory is created."""
    if not isinstance(feature, dict) or set(feature) - _FEATURE_FIELDS - {"data"}:
        raise _error("SCHEMA_INVALID", "Unknown feature member")
    if not _FEATURE_FIELDS.issubset(feature):
        raise _error("SCHEMA_INVALID", "Feature members are missing")
    _uuid(feature["id"])
    if (
        not isinstance(feature["name"], str)
        or not feature["name"].strip()
        or not 1 <= len(feature["name"]) <= 256
        or any(unicodedata.category(c) == "Cc" for c in feature["name"])
    ):
        raise _error("SCHEMA_INVALID", "Feature name needs 1..256 characters")
    if not isinstance(feature["op"], str) or feature["op"] not in REGISTRY:
        raise _error("CAPABILITY_UNAVAILABLE", "Unsupported mechanical operation")
    op = feature["op"]
    ports, names, data_names, output = REGISTRY[op]
    inputs = feature["inputs"]
    if ports is None:
        if (
            not isinstance(inputs, dict)
            or not 1 <= len(inputs) <= 128
            or set(inputs) != {f"part_{i}" for i in range(len(inputs))}
        ):
            raise _error("SCHEMA_INVALID", "Compound ports must be consecutive part_0..part_N")
        ports = {name: "solid" for name in inputs}
    _object(inputs, set(ports), "Feature.inputs")
    for port, wanted in ports.items():
        _uuid(inputs[port])
        if inputs[port] not in seen_types:
            raise _error("DEPENDENCY_NOT_READY", "Missing or forward feature dependency")
        if seen_types[inputs[port]] != wanted:
            raise _error("TYPE_MISMATCH", "Feature input has the wrong geometry category")
    parameters = _object(feature["parameters"], names, "Feature.parameters")
    numbers = {name: _number(value, name) for name, value in parameters.items()}
    data = _object(feature.get("data", {}), data_names, "Feature.data")
    for name, value in numbers.items():
        if name.endswith("_mm") and abs(value) > 1000:
            raise _error("OUTSIDE_QUALIFIED_ENVELOPE", f"{name} exceeds 1000 mm")
    if _AXIS.issubset(numbers):
        _axis(numbers)
    if op == "profile.circle@1":
        _positive(numbers["radius_mm"], "radius", lower=0.05, upper=500)
    elif op == "profile.rectangle@1":
        _positive(numbers["width_mm"], "width")
        _positive(numbers["height_mm"], "height")
    elif op == "profile.polygon@1":
        _polygon(_points(data["points_mm"], "polygon", 3, 256))
    elif op == "solid.extrude@1":
        _positive(numbers["distance_mm"], "distance", lower=1)
    elif op == "solid.revolve@1":
        if not 1e-6 <= numbers["angle_rad"] <= _TAU:
            raise _error("PARAMETER_INVALID", "Revolution angle requires 0.000001..2pi radians")
    elif op == "shape.transform@1":
        if abs(numbers["angle_rad"]) > _TAU:
            raise _error("PARAMETER_INVALID", "Transform angle exceeds +/-2pi radians")
    elif op in {"solid.fillet@1", "solid.chamfer@1"}:
        _positive(
            numbers["radius_mm"] if op == "solid.fillet@1" else numbers["distance_mm"],
            "edge treatment size",
        )
        _selector(data["selector"])
    elif op in {"shape.linear_pattern@1", "shape.circular_pattern@1"}:
        if type(data["count"]) is not int or not 2 <= data["count"] <= 128:
            raise _error("PARAMETER_INVALID", "Pattern count requires 2..128 instances")
        if type(data["fuse"]) is not bool:
            raise _error("SCHEMA_INVALID", "Pattern fuse must be boolean")
        if op == "shape.linear_pattern@1":
            if math.sqrt(sum(numbers[f"step_{axis}_mm"] ** 2 for axis in "xyz")) < 0.0001:
                raise _error("PARAMETER_INVALID", "Linear pattern step cannot be zero")
        else:
            angle = abs(numbers["step_angle_rad"])
            if angle < 1e-6 or angle * (data["count"] - 1) >= _TAU:
                raise _error("PARAMETER_INVALID", "Circular pattern span must be nonzero and <2pi")
    elif op == "solid.hole@1":
        _positive(numbers["diameter_mm"], "hole diameter")
        _positive(numbers["depth_mm"], "hole depth")
        _points(data["centers_mm"], "hole centers", 1, 128)
    return output


def _map(shape, kind):
    from OCP.collections import IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher
    from OCP.TopExp import TopExp

    indexed = IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher()
    TopExp.MapShapes_s(shape, kind, indexed)
    return indexed


def _solid_count(shape) -> int:
    from OCP.TopAbs import TopAbs_SOLID

    return _map(shape, TopAbs_SOLID).Extent()


def _volume(shape) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    properties = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, properties)
    return properties.Mass()


def _solid(shape, operation: str):
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID, TopAbs_VERTEX
    from OCP.TopExp import TopExp_Explorer

    from .kernel import _require_shape

    _require_shape(shape, operation=operation)
    mapped = _map(shape, TopAbs_SOLID)
    count = mapped.Extent()
    if count == 0:
        raise _error("EMPTY_GEOMETRY", "Operation produced no closed solid", operation=operation)
    if count > 4096:
        raise _error("RESOURCE_LIMIT", "Operation exceeds 4096 solid instances")
    if any(
        TopExp_Explorer(shape, kind, TopAbs_SOLID).More()
        for kind in (TopAbs_FACE, TopAbs_EDGE, TopAbs_VERTEX)
    ):
        raise _error("TYPE_MISMATCH", "Solid-bearing output contains loose faces, wires or points")
    volumes = [_volume(mapped.FindKey(i)) for i in range(1, count + 1)]
    if any(not math.isfinite(volume) or volume <= 1e-12 for volume in volumes):
        raise _error("GEOMETRY_INVALID", "Operation produced inverted or negligible-volume solids")
    return shape


def _boolean(base, tool, op: str, precision: PrecisionProfile):
    from OCP.BOPAlgo import BOPAlgo_BOP, BOPAlgo_COMMON, BOPAlgo_CUT, BOPAlgo_FUSE
    from OCP.Message import Message_Fail, Message_Warning

    from .kernel import _alerts, _history

    _solid(base, "boolean.base")
    _solid(tool, "boolean.tool")
    builder = BOPAlgo_BOP()
    builder.SetOperation(
        {
            "solid.cut@1": BOPAlgo_CUT,
            "solid.fuse@1": BOPAlgo_FUSE,
            "solid.common@1": BOPAlgo_COMMON,
        }[op]
    )
    builder.SetNonDestructive(True)
    builder.SetRunParallel(False)
    builder.SetToFillHistory(True)
    builder.SetFuzzyValue(precision.working_tolerance_mm)
    builder.AddArgument(base)
    builder.AddTool(tool)
    builder.Perform()
    diagnostic = {
        "op": op,
        "non_destructive": bool(builder.NonDestructive()),
        "fuzzy_tolerance_mm": builder.FuzzyValue(),
        "errors": _alerts(builder, Message_Fail),
        "warnings": _alerts(builder, Message_Warning),
    }
    if builder.HasErrors():
        raise _error("GEOMETRY_KERNEL_FAILURE", "OCCT Boolean failed", **diagnostic)
    if builder.HasWarnings():
        raise _error(
            "GEOMETRY_KERNEL_WARNING", "OCCT Boolean warning requires review", **diagnostic
        )
    shape = _solid(builder.Shape(), op)
    diagnostic["history"] = _history(builder, base, tool)
    return shape, diagnostic


def _profile(points):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.gp import gp_Pnt

    wire = BRepBuilderAPI_MakePolygon()
    for x, y in points:
        wire.Add(gp_Pnt(x, y, 0))
    wire.Close()
    if not wire.IsDone():
        raise _error("GEOMETRY_KERNEL_FAILURE", "OCCT could not construct polygon wire")
    face = BRepBuilderAPI_MakeFace(wire.Wire(), True)
    if not face.IsDone():
        raise _error("GEOMETRY_KERNEL_FAILURE", "OCCT could not construct planar profile")
    return face.Face()


def _transform(
    shape, translation=(0.0, 0.0, 0.0), axis=(0.0, 0.0, 1.0), angle=0.0, origin=(0.0, 0.0, 0.0)
):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec

    transform = gp_Trsf()
    transform.SetRotation(gp_Ax1(gp_Pnt(*origin), gp_Dir(*axis)), angle)
    # Translation follows rotation; retain the offset contributed by rotation axis.
    old = transform.TranslationPart()
    transform.SetTranslationPart(gp_Vec(*(old.Coord()[i] + translation[i] for i in range(3))))
    builder = BRepBuilderAPI_Transform(shape, transform, True, False)
    if not builder.IsDone():
        raise _error("GEOMETRY_KERNEL_FAILURE", "OCCT rigid transform failed")
    return builder.Shape()


def _compound(parts):
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.TopoDS import TopoDS_Compound

    shape = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(shape)
    for part in parts:
        # Each port is an occurrence, including two ports referencing one definition.
        # Copy topology so instance counts never collapse through shared TShape identity.
        builder.Add(shape, BRepBuilderAPI_Copy(part, True, False).Shape())
    return shape


def _parallel(first, second):
    return abs(abs(sum(a * b for a, b in zip(first, second))) - 1) <= _AXIS_TOL


def _select_edges(shape, selector: dict, precision: PrecisionProfile):
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GeomAbs import GeomAbs_Circle, GeomAbs_Line
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopoDS import TopoDS

    selector = _selector(selector)
    kind = selector["kind"]
    tolerance = precision.working_tolerance_mm
    edges, descriptions = [], []
    mapped = _map(shape, TopAbs_EDGE)
    for index in range(1, mapped.Extent() + 1):
        edge = TopoDS.Edge(mapped.FindKey(index))
        curve = BRepAdaptor_Curve(edge)
        curve_type = curve.GetType()
        description: dict = {"curve_type": str(curve_type)}
        if curve_type == GeomAbs_Line:
            line = curve.Line()
            direction, location = list(line.Direction().Coord()), list(line.Location().Coord())
            description.update(direction=direction, location_mm=location)
        elif curve_type == GeomAbs_Circle:
            circle = curve.Circle()
            direction, location = (
                list(circle.Axis().Direction().Coord()),
                list(circle.Location().Coord()),
            )
            description.update(axis=direction, center_mm=location, radius_mm=circle.Radius())
        elif kind == "plane":
            raise _error(
                "SELECTOR_UNSUPPORTED", "Plane selector needs analytical line/circle edges"
            )
        matched = kind == "all_edges"
        if kind == "line_parallel" and curve_type == GeomAbs_Line:
            matched = _parallel(direction, selector["axis"])
        elif kind in {"plane", "circle"} and curve_type in {GeomAbs_Line, GeomAbs_Circle}:
            axis = _AXES[selector["axis"]]
            axis_index = "xyz".index(selector["axis"])
            on_plane = abs(location[axis_index] - selector["coordinate_mm"]) <= tolerance
            if curve_type == GeomAbs_Line:
                matched = kind == "plane" and on_plane and abs(direction[axis_index]) <= _AXIS_TOL
            elif curve_type == GeomAbs_Circle:
                matched = on_plane and _parallel(direction, axis)
                if kind == "circle":
                    matched &= abs(circle.Radius() - selector["radius_mm"]) <= tolerance
        if matched:
            edges.append(edge)
            descriptions.append(description)
    expected, actual = selector["expected_count"], len(edges)
    if actual != expected:
        code = (
            "TOPOLOGY_REFERENCE_MISSING"
            if actual == 0
            else "TOPOLOGY_REFERENCE_AMBIGUOUS"
            if actual > expected
            else "TOPOLOGY_REFERENCE_MISMATCH"
        )
        raise _error(
            code,
            "Semantic edge selector cardinality differs",
            expected=expected,
            observed=actual,
            selector=selector,
        )
    return edges, {
        "selector": selector,
        "resolved_count": actual,
        "edges": descriptions,
        "scope": "geometric predicates re-resolved on this input B-Rep",
    }


def execute_feature(feature: dict, shapes: dict, precision: PrecisionProfile):
    """Evaluate one validated feature; result never mutates an upstream shape."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.BRepPrimAPI import (
        BRepPrimAPI_MakeCylinder,
        BRepPrimAPI_MakePrism,
        BRepPrimAPI_MakeRevol,
    )
    from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Vec

    from .kernel import _require_shape

    p, ports, data, op = (
        feature["parameters"],
        feature["inputs"],
        feature.get("data", {}),
        feature["op"],
    )
    diagnostic: dict = {"op": op}
    try:
        if op == "profile.circle@1":
            from OCP.BRepBuilderAPI import (
                BRepBuilderAPI_MakeEdge,
                BRepBuilderAPI_MakeFace,
                BRepBuilderAPI_MakeWire,
            )
            from OCP.gp import gp_Circ

            circle = gp_Circ(
                gp_Ax2(gp_Pnt(p["center_x_mm"], p["center_y_mm"], 0), gp_Dir(0, 0, 1)),
                p["radius_mm"],
            )
            edge = BRepBuilderAPI_MakeEdge(circle)
            wire = BRepBuilderAPI_MakeWire(edge.Edge())
            face = BRepBuilderAPI_MakeFace(wire.Wire())
            if not all((edge.IsDone(), wire.IsDone(), face.IsDone())):
                raise _error("GEOMETRY_KERNEL_FAILURE", "OCCT circle profile failed")
            shape = face.Face()
        elif op == "profile.rectangle@1":
            x, y, w, h = p["center_x_mm"], p["center_y_mm"], p["width_mm"] / 2, p["height_mm"] / 2
            shape = _profile([[x - w, y - h], [x + w, y - h], [x + w, y + h], [x - w, y + h]])
        elif op == "profile.polygon@1":
            shape = _profile(data["points_mm"])
        elif op == "solid.extrude@1":
            builder = BRepPrimAPI_MakePrism(
                shapes[ports["profile"]], gp_Vec(0, 0, p["distance_mm"]), True, True
            )
            shape = builder.Shape()
            if not builder.IsDone():
                raise _error("GEOMETRY_KERNEL_FAILURE", "OCCT extrusion failed")
        elif op == "solid.revolve@1":
            origin = [p[f"origin_{axis}_mm"] for axis in "xyz"]
            builder = BRepPrimAPI_MakeRevol(
                shapes[ports["profile"]],
                gp_Ax1(gp_Pnt(*origin), gp_Dir(*_axis(p))),
                p["angle_rad"],
                True,
            )
            shape = builder.Shape()
            if not builder.IsDone():
                raise _error("GEOMETRY_KERNEL_FAILURE", "OCCT revolution failed")
        elif op in {"solid.cut@1", "solid.fuse@1", "solid.common@1"}:
            shape, diagnostic = _boolean(
                shapes[ports["base"]], shapes[ports["tool"]], op, precision
            )
        elif op == "shape.transform@1":
            shape = _transform(
                shapes[ports["shape"]],
                [p[f"d{axis}_mm"] for axis in "xyz"],
                _axis(p),
                p["angle_rad"],
            )
        elif op in {"solid.fillet@1", "solid.chamfer@1"}:
            from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer, BRepFilletAPI_MakeFillet

            copied = BRepBuilderAPI_Copy(shapes[ports["shape"]], True, False).Shape()
            edges, selection = _select_edges(copied, data["selector"], precision)
            builder = (
                BRepFilletAPI_MakeFillet(copied)
                if op == "solid.fillet@1"
                else BRepFilletAPI_MakeChamfer(copied)
            )
            size = p["radius_mm"] if op == "solid.fillet@1" else p["distance_mm"]
            for edge in edges:
                builder.Add(size, edge)
            builder.Build()
            if not builder.IsDone():
                raise _error(
                    "GEOMETRY_KERNEL_FAILURE",
                    "OCCT edge treatment failed",
                    selection=selection,
                    size_mm=size,
                )
            shape = builder.Shape()
            diagnostic.update(selection=selection, contours=builder.NbContours())
        elif op in {"shape.linear_pattern@1", "shape.circular_pattern@1"}:
            original = _solid(shapes[ports["shape"]], "pattern.input")
            if _solid_count(original) * data["count"] > 4096:
                raise _error("RESOURCE_LIMIT", "Pattern exceeds 4096 solid instances")
            parts = []
            for i in range(data["count"]):
                if op == "shape.linear_pattern@1":
                    part = _transform(original, [i * p[f"step_{axis}_mm"] for axis in "xyz"])
                else:
                    part = _transform(
                        original,
                        axis=_axis(p),
                        angle=i * p["step_angle_rad"],
                        origin=[p[f"origin_{axis}_mm"] for axis in "xyz"],
                    )
                parts.append(part)
            if data["fuse"]:
                shape = parts[0]
                operations = []
                for part in parts[1:]:
                    shape, report = _boolean(shape, part, "solid.fuse@1", precision)
                    operations.append(report)
                diagnostic["booleans"] = operations
            else:
                shape = _compound(parts)
            diagnostic.update(instance_count=data["count"], fused=data["fuse"])
        elif op == "solid.hole@1":
            shape = shapes[ports["base"]]
            cuts = []
            for x, y in data["centers_mm"]:
                tool = BRepPrimAPI_MakeCylinder(
                    gp_Ax2(gp_Pnt(x, y, p["start_z_mm"]), gp_Dir(0, 0, 1)),
                    p["diameter_mm"] / 2,
                    p["depth_mm"],
                ).Shape()
                before = _volume(shape)
                shape, report = _boolean(shape, tool, "solid.cut@1", precision)
                removed = before - _volume(shape)
                if removed <= 1e-9:
                    raise _error("HOLE_MISSES_BASE", "Hole cutter removes no material")
                cuts.append({**report, "center_mm": [x, y], "removed_volume_mm3": removed})
            diagnostic["cuts"] = cuts
        elif op == "assembly.compound@1":
            parts = [shapes[ports[f"part_{i}"]] for i in range(len(ports))]
            if sum(_solid_count(part) for part in parts) > 4096:
                raise _error("RESOURCE_LIMIT", "Compound exceeds 4096 solid instances")
            shape = _compound(parts)
            diagnostic["part_count"] = len(parts)
        else:
            raise _error("CAPABILITY_UNAVAILABLE", "Unsupported mechanical operation")
        _require_shape(shape, operation=op)
        if REGISTRY[op][3] == "solid":
            _solid(shape, op)
            diagnostic.update(solid_count=_solid_count(shape), volume_mm3=_volume(shape))
        return shape, diagnostic
    except CadError:
        raise
    except Exception as exc:
        raise _error(
            "GEOMETRY_KERNEL_FAILURE",
            "OCCT feature execution failed",
            op=op,
            exception=type(exc).__name__,
            detail=str(exc)[:512],
        ) from exc
