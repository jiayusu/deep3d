"""Independent analytic B-Rep checks for the first flange fixture.

This verifier reads OCCT surfaces and topology, never construction metadata.
Its deliberately limited scope is a flange bounded by full cylindrical faces
and two planar caps. Split/approximated STEP surfaces are not silently treated
as equivalent. The measurement guard is a development acceptance guard band,
not an independently calibrated metrology uncertainty certificate.
"""

from __future__ import annotations

import math
from typing import Any

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.BRepGProp import BRepGProp
from OCP.collections import IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher
from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Plane
from OCP.gp import gp_Pnt, gp_Vec
from OCP.GProp import GProp_GProps
from OCP.TopAbs import (
    TopAbs_EDGE,
    TopAbs_FACE,
    TopAbs_OUT,
    TopAbs_REVERSED,
    TopAbs_SHELL,
    TopAbs_SOLID,
    TopAbs_VERTEX,
    TopAbs_WIRE,
)
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS, TopoDS_Shape

from .contracts import CadError, FlangeSpec, PrecisionProfile


def _subshapes(shape: TopoDS_Shape, kind: Any) -> list[TopoDS_Shape]:
    indexed = IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher()
    TopExp.MapShapes_s(shape, kind, indexed)
    return [indexed.FindKey(i) for i in range(1, indexed.Extent() + 1)]


def verify_flange(
    shape: TopoDS_Shape,
    expected: FlangeSpec,
    precision: PrecisionProfile | None = None,
    *,
    stage: str = "native",
) -> dict[str, Any]:
    """Measure actual analytic geometry against an independently supplied goal.

    ``pass`` means the bounded fixture checks passed with the stated guard
    band; it does not qualify arbitrary B-Reps or prove manufacturing quality.
    Unsupported representations produce ``inconclusive`` rather than a guessed
    geometric equivalence. A known violated check always produces ``fail``.
    """
    if stage not in {"native", "step"}:
        raise CadError("PARAMETER_INVALID", "Verification stage must be native or step")
    if not isinstance(expected, FlangeSpec):
        raise CadError("PARAMETER_INVALID", "Expected an independently supplied FlangeSpec")
    precision = precision or PrecisionProfile()
    if not isinstance(precision, PrecisionProfile):
        raise CadError("PARAMETER_INVALID", "Expected PrecisionProfile")

    limit = precision.critical_dimension_error_mm
    if stage == "step":
        limit = min(limit, precision.step_roundtrip_dimension_error_mm)
    guard = precision.measurement_error_budget_mm
    checks: list[dict[str, Any]] = []
    measurements: dict[str, Any] = {}

    def check(
        key: str,
        passed: bool,
        observed: Any,
        wanted: Any,
        *,
        details: Any = None,
        status: str | None = None,
    ) -> None:
        entry = {
            "id": key,
            "status": status or ("pass" if passed else "fail"),
            "observed": observed,
            "expected": wanted,
        }
        if details is not None:
            entry["details"] = details
        checks.append(entry)

    def dimension(key: str, observed: float, wanted: float) -> None:
        if not math.isfinite(observed):
            check(key, False, None, wanted, details="Non-finite analytic parameter")
            return
        error = abs(observed - wanted)
        check(
            key,
            error + guard <= limit,
            observed,
            wanted,
            details={
                "absolute_error_mm": error,
                "acceptance_guard_band_mm": guard,
                "effective_tolerance_mm": limit,
                "acceptance_rule": "absolute_error + guard_band <= effective_tolerance",
            },
        )

    def report() -> dict[str, Any]:
        statuses = {item["status"] for item in checks}
        status = (
            "fail"
            if "fail" in statuses
            else "inconclusive"
            if "inconclusive" in statuses
            else "pass"
        )
        return {
            "schema_version": "deep3d.flange-verification/1",
            "stage": stage,
            "status": status,
            "units": "mm",
            "coordinate_system": "right_handed_z_up",
            "expected_spec": expected.to_dict(),
            "precision_profile": precision.to_dict(),
            "measurement_model": {
                "method": "OCCT analytic surface parameters and B-Rep topology",
                "scope": "full axial cylinders and two planar caps of the flange fixture",
                "acceptance_guard_band_mm": guard,
                "independently_calibrated_metrology_bound": False,
                "industrial_qualification": "not_established",
                "volume_is_crosscheck_only": True,
            },
            "checks": checks,
            "measurements": measurements,
        }

    if not isinstance(shape, TopoDS_Shape) or shape.IsNull():
        check("shape.non_null", False, None, "non-null B-Rep")
        return report()

    try:
        analyzer = BRepCheck_Analyzer(shape, True, False, True)
        check(
            "shape.brep_valid",
            analyzer.IsValid(),
            analyzer.IsValid(),
            True,
            details={"geometry_controls": True, "exact_method": True, "parallel": False},
        )
        solids = _subshapes(shape, TopAbs_SOLID)
        shells = _subshapes(shape, TopAbs_SHELL)
        faces = _subshapes(shape, TopAbs_FACE)
        check("shape.solid_count", len(solids) == 1, len(solids), 1)
        check("shape.shell_count", len(shells) == 1, len(shells), 1)
        closure = [BRep_Tool.IsClosed_s(shell) for shell in shells]
        check("shape.closed_shells", bool(closure) and all(closure), closure, "all closed")
        if len(solids) == 1:
            classifier = BRepClass3d_SolidClassifier(solids[0])
            classifier.PerformInfinitePoint(precision.working_tolerance_mm)
            is_out = classifier.State() == TopAbs_OUT
            check(
                "shape.outward_orientation",
                is_out,
                str(classifier.State()),
                "TopAbs_OUT at infinity",
            )
            # STEP readers may return a compound containing the one solid.
            # That wrapper is acceptable; stray faces/edges/vertices are not.
            for label, kind in (
                ("face", TopAbs_FACE),
                ("edge", TopAbs_EDGE),
                ("vertex", TopAbs_VERTEX),
            ):
                total = len(_subshapes(shape, kind))
                inside = len(_subshapes(solids[0], kind))
                check(f"shape.no_stray_{label}", total == inside, total - inside, 0)

        tolerances: dict[str, float] = {}
        for label, kind, convert in (
            ("face", TopAbs_FACE, TopoDS.Face),
            ("edge", TopAbs_EDGE, TopoDS.Edge),
            ("vertex", TopAbs_VERTEX, TopoDS.Vertex),
        ):
            values = [
                float(BRep_Tool.Tolerance_s(convert(item))) for item in _subshapes(shape, kind)
            ]
            maximum = max(values, default=0.0)
            if not all(math.isfinite(value) and value >= 0 for value in values):
                check(f"tolerance.{label}", False, None, "finite nonnegative tolerances")
            else:
                tolerances[label] = maximum
                check(
                    f"tolerance.{label}",
                    maximum <= precision.max_subshape_tolerance_mm,
                    maximum,
                    {"maximum_mm": precision.max_subshape_tolerance_mm},
                )
        measurements["max_subshape_tolerances_mm"] = tolerances

        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape, props, True, False, False)
        volume = float(props.Mass())
        if not math.isfinite(volume):
            check("shape.volume", False, None, "finite positive volume")
        else:
            measurements["volume_mm3"] = volume
            expected_volume = expected.expected_volume_mm3
            relative_error = abs(volume - expected_volume) / expected_volume
            check("shape.positive_volume", volume > 0, volume, "> 0 mm3")
            check(
                "crosscheck.volume",
                relative_error <= precision.volume_relative_error,
                volume,
                expected_volume,
                details={
                    "relative_error": relative_error,
                    "limit": precision.volume_relative_error,
                },
            )
            cog = list(props.CentreOfMass().Coord())
            if all(math.isfinite(value) for value in cog):
                measurements["center_of_mass_mm"] = cog

        cylinders: list[dict[str, Any]] = []
        planes: list[dict[str, Any]] = []
        unsupported: list[str] = []
        for face_index, raw_face in enumerate(faces):
            face = TopoDS.Face(raw_face)
            adaptor = BRepAdaptor_Surface(face, True)
            surface_type = adaptor.GetType()
            if surface_type == GeomAbs_Cylinder:
                cylinder = adaptor.Cylinder()
                origin = cylinder.Location().Coord()
                axis = cylinder.Axis().Direction().Coord()
                bounds = (
                    adaptor.FirstUParameter(),
                    adaptor.LastUParameter(),
                    adaptor.FirstVParameter(),
                    adaptor.LastVParameter(),
                )
                radius = cylinder.Radius()
                if not all(math.isfinite(value) for value in (*origin, *axis, *bounds, radius)):
                    check(f"surface.{face_index}.finite", False, None, "finite analytic parameters")
                    continue
                if abs(axis[2]) < 1e-12:
                    check(f"surface.{face_index}.axis", False, list(axis), "parallel to world Z")
                    continue
                center = [
                    origin[0] - origin[2] * axis[0] / axis[2],
                    origin[1] - origin[2] * axis[1] / axis[2],
                ]
                endpoint_a = adaptor.Value(bounds[0], bounds[2])
                endpoint_b = adaptor.Value(bounds[0], bounds[3])
                z_min, z_max = sorted((endpoint_a.Z(), endpoint_b.Z()))
                # Face orientation is relative to the surface UV frame. A
                # prism can use an indirect cylinder frame, so measure the
                # oriented normal instead of equating FORWARD with outward.
                point, du, dv = gp_Pnt(), gp_Vec(), gp_Vec()
                adaptor.D1(
                    (bounds[0] + bounds[1]) / 2,
                    (bounds[2] + bounds[3]) / 2,
                    point,
                    du,
                    dv,
                )
                normal = du.Crossed(dv)
                if face.Orientation() == TopAbs_REVERSED:
                    normal.Reverse()
                displacement = [point.Coord(i + 1) - origin[i] for i in range(3)]
                axial = sum(displacement[i] * axis[i] for i in range(3))
                radial = [displacement[i] - axial * axis[i] for i in range(3)]
                normal_dot_radial = sum(normal.Coord(i + 1) * radial[i] for i in range(3))
                if not math.isfinite(normal_dot_radial) or abs(normal_dot_radial) < 1e-12:
                    check(f"surface.{face_index}.normal", False, None, "nonzero radial normal")
                record = {
                    "face_index_in_this_artifact": face_index,
                    "radius_mm": radius,
                    "center_at_z0_mm": center,
                    "axis_direction": list(axis),
                    "axis_lateral_drift_mm": math.hypot(axis[0], axis[1])
                    * expected.thickness_mm
                    / abs(axis[2]),
                    "z_min_mm": z_min,
                    "z_max_mm": z_max,
                    "angular_span_rad": bounds[1] - bounds[0],
                    "outward_radial": normal_dot_radial > 0,
                    "inward_radial": normal_dot_radial < 0,
                }
                cylinders.append(record)
                if abs(record["angular_span_rad"] - math.tau) > 1e-9:
                    check(
                        f"surface.{face_index}.full_cylinder",
                        False,
                        record["angular_span_rad"],
                        math.tau,
                        status="inconclusive",
                        details=("Split/partial cylinders are outside this verifier profile"),
                    )
            elif surface_type == GeomAbs_Plane:
                plane = adaptor.Plane()
                location = plane.Location().Coord()
                direction = list(plane.Axis().Direction().Coord())
                if face.Orientation() == TopAbs_REVERSED:
                    direction = [-value for value in direction]
                if not all(math.isfinite(value) for value in (*location, *direction)):
                    check(f"surface.{face_index}.finite", False, None, "finite plane parameters")
                    continue
                planes.append(
                    {
                        "face_index_in_this_artifact": face_index,
                        "plane_origin_mm": list(location),
                        "outward_normal": direction,
                        "wire_count": len(_subshapes(face, TopAbs_WIRE)),
                    }
                )
            else:
                unsupported.append(str(surface_type))
        measurements["cylinders"] = cylinders
        measurements["planes"] = planes
        if unsupported:
            check(
                "surface.supported_analytic_types",
                False,
                unsupported,
                ["plane", "cylinder"],
                status="inconclusive",
            )

        check(
            "surface.cylinder_count",
            len(cylinders) == expected.hole_count + 2,
            len(cylinders),
            expected.hole_count + 2,
        )
        check("surface.plane_count", len(planes) == 2, len(planes), 2)
        outer = [item for item in cylinders if item["outward_radial"]]
        holes = [item for item in cylinders if item["inward_radial"]]
        check("surface.outer_count", len(outer) == 1, len(outer), 1)
        check(
            "hole.count", len(holes) == expected.hole_count + 1, len(holes), expected.hole_count + 1
        )

        def cylinder_dimensions(
            prefix: str, cylinder: dict[str, Any], center: tuple[float, float], diameter: float
        ) -> None:
            dimension(prefix + ".diameter", cylinder["radius_mm"] * 2, diameter)
            dimension(prefix + ".center_x", cylinder["center_at_z0_mm"][0], center[0])
            dimension(prefix + ".center_y", cylinder["center_at_z0_mm"][1], center[1])
            dimension(prefix + ".axis_drift", cylinder["axis_lateral_drift_mm"], 0.0)
            dimension(prefix + ".z_min", cylinder["z_min_mm"], 0.0)
            dimension(prefix + ".z_max", cylinder["z_max_mm"], expected.thickness_mm)

        if len(outer) == 1:
            cylinder_dimensions("outer", outer[0], (0.0, 0.0), expected.outer_diameter_mm)
        remaining = list(holes)
        targets = [("center_hole", (0.0, 0.0), expected.center_hole_diameter_mm)] + [
            (f"bolt_hole.{index}", center, expected.bolt_hole_diameter_mm)
            for index, center in enumerate(expected.bolt_centers_mm())
        ]
        for name, center, diameter in targets:
            if not remaining:
                check(name + ".present", False, False, True)
                continue
            # Assignment uses position, never radius, so a wrong radius cannot
            # disappear from the measured hole set by changing its classification.
            nearest = min(remaining, key=lambda item: math.dist(item["center_at_z0_mm"], center))
            remaining.remove(nearest)
            cylinder_dimensions(name, nearest, center, diameter)

        for index, plane in enumerate(sorted(planes, key=lambda item: item["plane_origin_mm"][2])):
            prefix = f"cap.{index}"
            dimension(
                prefix + ".normal_xy_drift",
                math.hypot(*plane["outward_normal"][:2]) * expected.outer_diameter_mm,
                0.0,
            )
            if index < 2:
                dimension(
                    prefix + ".z", plane["plane_origin_mm"][2], (0.0, expected.thickness_mm)[index]
                )
                expected_sign = -1 if index == 0 else 1
                check(
                    prefix + ".outward_normal",
                    plane["outward_normal"][2] * expected_sign > 0,
                    plane["outward_normal"],
                    [0.0, 0.0, expected_sign],
                )
            check(
                prefix + ".wire_count",
                plane["wire_count"] == expected.hole_count + 2,
                plane["wire_count"],
                expected.hole_count + 2,
            )
    except Exception as exc:
        check(
            "verifier.occt_measurement_completed",
            False,
            type(exc).__name__,
            "all required checks completed",
            status="inconclusive",
            details=str(exc),
        )
    return report()
