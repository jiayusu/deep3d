import math

import pytest
from deep3d_cad.contracts import CadError, FlangeSpec
from deep3d_cad.kernel import (
    _brep_bytes,
    _cut,
    build_flange,
    export_step,
    import_step,
    read_brep,
    write_brep,
)
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
from OCP.BRepGProp import BRepGProp
from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Dir, gp_Pln, gp_Pnt
from OCP.GProp import GProp_GProps
from OCP.TopoDS import TopoDS_Shape


def _volume(shape):
    properties = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, properties)
    return properties.Mass()


def test_build_real_flange_and_history():
    result = build_flange(FlangeSpec())
    assert _volume(result.shape) == pytest.approx(26532 * math.pi, rel=1e-10)
    assert len(result.operations) == 6
    for operation in result.operations[1:]:
        assert operation["non_destructive"] is True
        assert operation["errors"] == operation["warnings"] == []
        assert operation["history"]["available"] is True
        assert operation["history"]["counts"]["modified"] > 0
        assert operation["history"]["relations"]


def test_boolean_does_not_mutate_inputs():
    argument = BRepPrimAPI_MakeCylinder(50, 12).Shape()
    tool = BRepPrimAPI_MakeCylinder(15, 12).Shape()
    before_argument, before_tool = _brep_bytes(argument), _brep_bytes(tool)
    result, _ = _cut(argument, tool, "qualification")
    assert _brep_bytes(argument) == before_argument
    assert _brep_bytes(tool) == before_tool
    assert _volume(result) < _volume(argument)


def test_explicit_working_tolerance_is_used_in_every_cut(tmp_path):
    result = build_flange(FlangeSpec(), working_tolerance_mm=2e-5)
    assert all(op["fuzzy_tolerance_mm"] == 2e-5 for op in result.operations[1:])
    report = export_step(result.shape, tmp_path / "profile.step", working_tolerance_mm=2e-5)
    assert report["writer_tolerance_mm"] == 2e-5


@pytest.mark.parametrize("tolerance", [float("nan"), float("inf"), -1, 0, True, "0.00001", 0.01])
def test_unqualified_working_tolerances_are_rejected(tolerance):
    with pytest.raises(CadError):
        build_flange(FlangeSpec(), working_tolerance_mm=tolerance)


def test_valid_empty_boolean_is_distinct_from_kernel_failure():
    argument = BRepPrimAPI_MakeCylinder(50, 12).Shape()
    with pytest.raises(CadError) as error:
        _cut(argument, argument, "empty_cut")
    assert error.value.code == "EMPTY_GEOMETRY"


def test_real_kernel_failure_exposes_actual_occt_alert():
    argument = BRepPrimAPI_MakeCylinder(50, 12).Shape()
    invalid_tool = BRepBuilderAPI_MakeFace(
        gp_Pln(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), -10, 10, -10, 10
    ).Shape()
    with pytest.raises(CadError) as error:
        _cut(argument, invalid_tool, "solid_minus_face")
    assert error.value.code == "GEOMETRY_KERNEL_FAILURE"
    assert error.value.details["errors"] == ["BOPAlgo_AlertBOPNotAllowed"]


def test_brep_roundtrip(tmp_path):
    shape = build_flange(FlangeSpec()).shape
    path = tmp_path / "flange.brep"
    write_brep(shape, path)
    assert _volume(read_brep(path)) == pytest.approx(_volume(shape), rel=1e-12)


def test_step_ap214_millimetres_roundtrip(tmp_path):
    shape = build_flange(FlangeSpec()).shape
    path = tmp_path / "flange.step"
    profile = export_step(shape, path)
    text = path.read_text(encoding="ascii")
    assert "AUTOMOTIVE_DESIGN" in text
    assert "SI_UNIT(.MILLI.,.METRE.)" in text
    assert profile["schema"] == "AP214IS"
    assert profile["unit"] == "mm"
    assert profile["errors"] == profile["warnings"] == []
    assert _volume(import_step(path)) == pytest.approx(_volume(shape), rel=1e-8)


def test_step_rejects_undeclared_or_unsupported_units(tmp_path):
    shape = build_flange(FlangeSpec()).shape
    path = tmp_path / "flange.step"
    export_step(shape, path)
    original = path.read_text(encoding="ascii")
    path.write_text(
        original.replace("SI_UNIT(.MILLI.,.METRE.)", "SI_UNIT($,.METRE.)"), encoding="ascii"
    )
    with pytest.raises(CadError) as error:
        import_step(path)
    assert error.value.code == "STEP_UNIT_UNSUPPORTED"


@pytest.mark.parametrize("reader,suffix", [(read_brep, "brep"), (import_step, "step")])
def test_missing_artifact_is_structured(reader, suffix, tmp_path):
    with pytest.raises(CadError) as error:
        reader(tmp_path / f"missing.{suffix}")
    assert error.value.code == "ARTIFACT_NOT_FOUND"


@pytest.mark.parametrize("reader,suffix", [(read_brep, "brep"), (import_step, "step")])
def test_corrupt_artifact_is_rejected(reader, suffix, tmp_path):
    path = tmp_path / f"broken.{suffix}"
    path.write_text("this is not a CAD file", encoding="ascii")
    with pytest.raises(CadError):
        reader(path)


def test_null_geometry_is_rejected_before_export(tmp_path):
    with pytest.raises(CadError) as error:
        export_step(TopoDS_Shape(), tmp_path / "bad.step")
    assert error.value.code == "EMPTY_GEOMETRY"
    assert not (tmp_path / "bad.step").exists()
