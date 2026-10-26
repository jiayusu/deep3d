from __future__ import annotations

import json
import math
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from deep3d_cad.contracts import CadError, FlangeSpec, PrecisionProfile
from jsonschema import Draft202012Validator, ValidationError

ROOT = Path(__file__).resolve().parents[1]


def test_standard_fixture_and_analytic_reference() -> None:
    payload = json.loads((ROOT / "fixtures/flange/standard.json").read_text(encoding="utf-8"))
    spec = FlangeSpec.from_dict(payload)
    assert spec == FlangeSpec()
    assert spec.to_dict() == payload
    assert spec.expected_volume_mm3 == pytest.approx(26532 * math.pi, abs=1e-9)
    for actual, expected in zip(
        spec.bolt_centers_mm(), [(35, 0), (0, 35), (-35, 0), (0, -35)], strict=True
    ):
        assert actual == pytest.approx(expected, abs=1e-12)
    with pytest.raises(FrozenInstanceError):
        spec.thickness_mm = 20  # type: ignore[misc]


def test_goal_does_not_change_when_candidate_parameters_change() -> None:
    goal = FlangeSpec()
    candidate = replace(goal, thickness_mm=14)
    assert goal.thickness_mm == 12
    assert candidate.expected_volume_mm3 / goal.expected_volume_mm3 == pytest.approx(14 / 12)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("outer_diameter_mm", True),
        ("thickness_mm", "12"),
        ("center_hole_diameter_mm", math.nan),
        ("bolt_hole_diameter_mm", math.inf),
        ("start_angle_rad", -math.inf),
        ("outer_diameter_mm", 100000),
        ("thickness_mm", 0.012),
        ("bolt_hole_diameter_mm", -8),
        ("center_hole_diameter_mm", 0),
        ("hole_count", True),
        ("hole_count", 4.0),
        ("hole_count", 2),
        ("hole_count", 33),
        ("start_angle_rad", 7),
    ],
)
def test_invalid_parameters_rejected_by_constructor_and_payload(name: str, value: object) -> None:
    with pytest.raises(CadError):
        FlangeSpec(**{name: value})
    payload = FlangeSpec().to_dict()
    payload[name] = value
    with pytest.raises(CadError):
        FlangeSpec.from_dict(payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"bolt_circle_diameter_mm": 95},  # Breaks outer stock wall.
        {"center_hole_diameter_mm": 63},  # Overlaps center and mounting holes.
        {"hole_count": 32},  # Adjacent mounting holes overlap.
    ],
)
def test_geometric_cross_field_constraints(changes: dict[str, object]) -> None:
    with pytest.raises(CadError, match="Holes must be distinct"):
        FlangeSpec(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": "deep3d.flange/2"},
        {"units": "m"},
        {"coordinate_system": "right_handed_y_up"},
        {"arbitrary_python": "print('no')"},
    ],
)
def test_protocol_metadata_and_unknown_fields(changes: dict[str, object]) -> None:
    with pytest.raises(CadError):
        FlangeSpec.from_dict({**FlangeSpec().to_dict(), **changes})


def test_missing_field_is_not_silently_defaulted() -> None:
    payload = FlangeSpec().to_dict()
    del payload["units"]
    with pytest.raises(CadError) as error:
        FlangeSpec.from_dict(payload)
    assert error.value.details["missing"] == ["units"]


@pytest.mark.parametrize("value", [None, [], "{}", {1: "bad"}])
def test_payload_must_be_string_keyed_object(value: object) -> None:
    with pytest.raises(CadError):
        FlangeSpec.from_dict(value)


def test_signed_zero_is_normalized_and_rotated_centers_are_supported() -> None:
    spec = FlangeSpec(start_angle_rad=-0.0)
    assert math.copysign(1, spec.start_angle_rad) == 1
    rotated = replace(spec, start_angle_rad=math.pi / 4)
    assert rotated.bolt_centers_mm()[0] == pytest.approx((35 / math.sqrt(2),) * 2)


def test_profile_roundtrip_and_immutability() -> None:
    profile = PrecisionProfile()
    assert PrecisionProfile.from_dict(profile.to_dict()) == profile
    assert profile.critical_dimension_error_mm < profile.step_roundtrip_dimension_error_mm
    with pytest.raises(FrozenInstanceError):
        profile.working_tolerance_mm = 0.1  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes",
    [
        {"working_tolerance_mm": 0},
        {"max_subshape_tolerance_mm": -1},
        {"critical_dimension_error_mm": True},
        {"step_roundtrip_dimension_error_mm": math.inf},
        {"volume_relative_error": math.nan},
        {"volume_relative_error": 1},
        {"measurement_error_budget_mm": 0.001},
        {"working_tolerance_mm": 0.01},
    ],
)
def test_precision_profile_rejects_unusable_thresholds(changes: dict[str, object]) -> None:
    with pytest.raises(CadError):
        PrecisionProfile(**changes)


def test_working_tolerance_matches_kernel_envelope_without_changing_dimension_limits() -> None:
    profile = PrecisionProfile(working_tolerance_mm=0.001)
    assert profile.critical_dimension_error_mm == PrecisionProfile().critical_dimension_error_mm
    assert (
        profile.step_roundtrip_dimension_error_mm
        == PrecisionProfile().step_roundtrip_dimension_error_mm
    )
    payload = profile.to_dict()
    schema = json.loads((ROOT / "specs/precision-profile.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    validator.validate(payload)
    payload["working_tolerance_mm"] = math.nextafter(0.001, math.inf)
    # Raise the independent shape-tolerance ceiling to isolate the kernel bound.
    payload["max_subshape_tolerance_mm"] = 0.01
    with pytest.raises(CadError) as error:
        PrecisionProfile.from_dict(payload)
    assert error.value.code == "OUTSIDE_QUALIFIED_ENVELOPE"
    with pytest.raises(ValidationError):
        validator.validate(payload)


@pytest.mark.parametrize(
    ("schema_file", "payload"),
    [
        ("flange.schema.json", FlangeSpec().to_dict()),
        ("precision-profile.schema.json", PrecisionProfile().to_dict()),
    ],
)
def test_schemas_accept_full_contract_and_reject_extra_fields(
    schema_file: str, payload: dict[str, object]
) -> None:
    schema = json.loads((ROOT / "specs" / schema_file).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate(payload)
    with pytest.raises(ValidationError):
        validator.validate({**payload, "unversioned_option": True})


def test_error_is_machine_readable() -> None:
    error = CadError("PARAMETER_INVALID", "bad dimension", {"field": "thickness_mm"})
    assert str(error) == "bad dimension"
    assert error.to_dict() == {
        "code": "PARAMETER_INVALID",
        "message": "bad dimension",
        "details": {"field": "thickness_mm"},
    }
