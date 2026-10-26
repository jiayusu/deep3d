"""Adversarial tests use actual B-Reps, not forged measurement metadata."""

from __future__ import annotations

import json
import math

import pytest
from deep3d_cad.contracts import CadError, FlangeSpec, PrecisionProfile
from deep3d_cad.verifier import verify_flange
from OCP.BRep import BRep_Builder
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_Transform
from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakeSphere
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.TopoDS import TopoDS_Compound, TopoDS_Shape


def _actual_flange(
    *,
    outer_diameter: float = 100,
    thickness: float = 12,
    center_diameter: float = 30,
    bolt_diameter: float = 8,
    omit_bolt: int | None = None,
    displaced_bolt: int | None = None,
    displacement: float = 0,
    blind_bolt: int | None = None,
) -> TopoDS_Shape:
    shape = BRepPrimAPI_MakeCylinder(outer_diameter / 2, thickness).Shape()
    holes = [(0.0, 0.0, center_diameter / 2, thickness + 2)]
    for index in range(4):
        if index == omit_bolt:
            continue
        angle = index * math.tau / 4
        x = 35 * math.cos(angle)
        y = 35 * math.sin(angle)
        if index == displaced_bolt:
            x += displacement
        depth = thickness / 2 if index == blind_bolt else thickness + 2
        holes.append((x, y, bolt_diameter / 2, depth))
    for x, y, radius, depth in holes:
        tool = BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(x, y, -1), gp_Dir(0, 0, 1)), radius, depth
        ).Shape()
        operation = BRepAlgoAPI_Cut(shape, tool)
        operation.SetNonDestructive(True)
        operation.Build()
        assert operation.IsDone()
        shape = operation.Shape()
    return shape


def _failed_ids(report: dict) -> set[str]:
    return {check["id"] for check in report["checks"] if check["status"] == "fail"}


def test_standard_flange_is_measured_without_builder_metadata():
    report = verify_flange(_actual_flange(), FlangeSpec())
    assert report["status"] == "pass"
    assert report["measurements"]["volume_mm3"] == pytest.approx(26532 * math.pi)
    assert len(report["measurements"]["cylinders"]) == 6
    assert len(report["checks"]) == len({item["id"] for item in report["checks"]})
    assert report["measurement_model"]["industrial_qualification"] == "not_established"
    assert report["measurement_model"]["independently_calibrated_metrology_bound"] is False
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize(
    ("parameters", "expected_failure"),
    [
        ({"outer_diameter": 102}, "outer.diameter"),
        ({"thickness": 14}, "outer.z_max"),
        ({"center_diameter": 32}, "center_hole.diameter"),
        ({"bolt_diameter": 9}, "bolt_hole.0.diameter"),
        ({"omit_bolt": 2}, "hole.count"),
        ({"displaced_bolt": 0, "displacement": 1}, "bolt_hole.0.center_x"),
        ({"blind_bolt": 0}, "bolt_hole.0.z_max"),
    ],
)
def test_actual_geometry_defects_are_rejected(parameters, expected_failure):
    report = verify_flange(_actual_flange(**parameters), FlangeSpec())
    assert report["status"] == "fail"
    assert expected_failure in _failed_ids(report)


def test_moved_hole_can_preserve_volume_but_still_fails_position():
    report = verify_flange(_actual_flange(displaced_bolt=0, displacement=1), FlangeSpec())
    checks = {item["id"]: item for item in report["checks"]}
    assert checks["crosscheck.volume"]["status"] == "pass"
    assert checks["bolt_hole.0.center_x"]["status"] == "fail"
    assert report["status"] == "fail"


def test_whole_part_translation_fails_datum_contract():
    transform = gp_Trsf()
    transform.SetTranslation(gp_Vec(1, 0, 2))
    shifted = BRepBuilderAPI_Transform(_actual_flange(), transform, True).Shape()
    report = verify_flange(shifted, FlangeSpec())
    assert report["status"] == "fail"
    assert {"outer.center_x", "outer.z_min", "cap.0.z"} <= _failed_ids(report)


def test_millimeter_meter_scale_mistake_is_rejected():
    transform = gp_Trsf()
    transform.SetScale(gp_Pnt(0, 0, 0), 0.001)
    tiny = BRepBuilderAPI_Transform(_actual_flange(), transform, True).Shape()
    report = verify_flange(tiny, FlangeSpec())
    assert report["status"] == "fail"
    assert "outer.diameter" in _failed_ids(report)


def test_additional_solid_is_rejected():
    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, _actual_flange())
    builder.Add(
        compound,
        BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(200, 0, 0), gp_Dir(0, 0, 1)), 2, 3).Shape(),
    )
    report = verify_flange(compound, FlangeSpec())
    assert report["status"] == "fail"
    assert "shape.solid_count" in _failed_ids(report)


def test_inward_oriented_solid_is_rejected():
    reversed_shape = _actual_flange().Reversed()
    report = verify_flange(reversed_shape, FlangeSpec())
    assert report["status"] == "fail"
    assert "shape.outward_orientation" in _failed_ids(report)
    assert "shape.positive_volume" in _failed_ids(report)


def test_stray_edge_is_rejected_even_when_the_one_solid_is_correct():
    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, _actual_flange())
    builder.Add(
        compound,
        BRepBuilderAPI_MakeEdge(gp_Pnt(200, 0, 0), gp_Pnt(210, 0, 0)).Edge(),
    )
    report = verify_flange(compound, FlangeSpec())
    assert report["status"] == "fail"
    assert "shape.no_stray_edge" in _failed_ids(report)


def test_null_shape_fails_without_throwing_or_nan_json():
    report = verify_flange(TopoDS_Shape(), FlangeSpec())
    assert report["status"] == "fail"
    assert "shape.non_null" in _failed_ids(report)
    json.dumps(report, allow_nan=False)


def test_unknown_surface_is_not_silently_accepted():
    report = verify_flange(BRepPrimAPI_MakeSphere(10).Shape(), FlangeSpec())
    assert report["status"] != "pass"
    assert any(
        item["id"] == "surface.supported_analytic_types" and item["status"] == "inconclusive"
        for item in report["checks"]
    )


def test_acceptance_guard_rejects_dimension_near_limit():
    report = verify_flange(_actual_flange(outer_diameter=100.00095), FlangeSpec())
    check = next(item for item in report["checks"] if item["id"] == "outer.diameter")
    assert check["details"]["absolute_error_mm"] < 0.001
    assert check["status"] == "fail"
    assert check["details"]["acceptance_guard_band_mm"] == 0.0001


def test_step_profile_cannot_relax_goal_dimension_tolerance():
    report = verify_flange(
        _actual_flange(outer_diameter=100.002),
        FlangeSpec(),
        PrecisionProfile(),
        stage="step",
    )
    check = next(item for item in report["checks"] if item["id"] == "outer.diameter")
    assert report["status"] == "fail"
    assert check["details"]["effective_tolerance_mm"] == 0.001


def test_invalid_verification_stage_is_rejected():
    with pytest.raises(CadError, match="stage"):
        verify_flange(TopoDS_Shape(), FlangeSpec(), stage="mesh")


def test_kernel_step_roundtrip_is_measured_from_reimported_geometry(tmp_path):
    from deep3d_cad.kernel import build_flange, export_step, import_step

    path = tmp_path / "flange.step"
    export_step(build_flange(FlangeSpec()).shape, path)
    report = verify_flange(import_step(path), FlangeSpec(), stage="step")
    assert report["status"] == "pass"
