"""Strict contracts for the first, deliberately bounded analytic CAD fixture.

These definitions describe both a requested goal and a modeled candidate. The
caller must keep those separate; a candidate's own parameters are not evidence
that it satisfies an independently supplied goal.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any, ClassVar


class CadError(Exception):
    """A machine-readable boundary error suitable for worker JSON responses."""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details) if details is not None else {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def _finite_number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CadError("PARAMETER_INVALID", f"{name} must be a finite number", {"field": name})
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise CadError("PARAMETER_INVALID", f"{name} must be finite", {"field": name}) from exc
    if not math.isfinite(result):
        raise CadError("PARAMETER_INVALID", f"{name} must be finite", {"field": name})
    # Canonicalize signed zero before it reaches hashing or geometry adapters.
    return 0.0 if result == 0.0 else result


def _explicit_payload(
    value: Any, expected_fields: set[str], constants: Mapping[str, str]
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise CadError("SCHEMA_INVALID", "Expected a JSON object with string field names")
    expected = expected_fields | set(constants)
    actual = set(value)
    if expected != actual:
        raise CadError(
            "SCHEMA_INVALID",
            "Payload fields must exactly match the versioned contract",
            {"missing": sorted(expected - actual), "unknown": sorted(actual - expected)},
        )
    for key, expected_value in constants.items():
        if value[key] != expected_value:
            code = "SCHEMA_UNSUPPORTED" if key == "schema_version" else "SCHEMA_INVALID"
            raise CadError(
                code,
                f"Unsupported {key}",
                {"field": key, "expected": expected_value},
            )
    return {name: value[name] for name in expected_fields}


@dataclass(frozen=True)
class FlangeSpec:
    """Fully dimensioned, axis-aligned flange; dimensions are millimeters.

    Qualification envelope: outer diameter and thickness 1..1000 mm, circular
    holes and all specified material ligaments at least 0.1 mm. This is an
    application fixture restriction, not a claim about the OCCT kernel's limit.
    The lower plane is z=0 and the symmetry axis is the world Z axis.
    """

    SCHEMA_VERSION: ClassVar[str] = "deep3d.flange/1"
    UNITS: ClassVar[str] = "mm"
    COORDINATE_SYSTEM: ClassVar[str] = "right_handed_z_up"

    outer_diameter_mm: float = 100.0
    thickness_mm: float = 12.0
    center_hole_diameter_mm: float = 30.0
    bolt_hole_diameter_mm: float = 8.0
    bolt_circle_diameter_mm: float = 70.0
    hole_count: int = 4
    start_angle_rad: float = 0.0

    def __post_init__(self) -> None:
        for field in fields(self):
            if field.name == "hole_count":
                continue
            object.__setattr__(
                self, field.name, _finite_number(field.name, getattr(self, field.name))
            )
        if isinstance(self.hole_count, bool) or not isinstance(self.hole_count, int):
            raise CadError("PARAMETER_INVALID", "hole_count must be an integer")
        if not 3 <= self.hole_count <= 32:
            raise CadError("OUTSIDE_QUALIFIED_ENVELOPE", "hole_count must be between 3 and 32")
        for name in ("outer_diameter_mm", "thickness_mm"):
            if not 1.0 <= getattr(self, name) <= 1000.0:
                raise CadError(
                    "OUTSIDE_QUALIFIED_ENVELOPE",
                    f"{name} must be within 1..1000 mm",
                    {"field": name},
                )
        for name in (
            "center_hole_diameter_mm",
            "bolt_hole_diameter_mm",
            "bolt_circle_diameter_mm",
        ):
            if not 0.1 <= getattr(self, name) <= 1000.0:
                raise CadError(
                    "OUTSIDE_QUALIFIED_ENVELOPE",
                    f"{name} must be within 0.1..1000 mm",
                    {"field": name},
                )
        if abs(self.start_angle_rad) > math.tau:
            raise CadError(
                "OUTSIDE_QUALIFIED_ENVELOPE", "start_angle_rad must be within [-2*pi, 2*pi]"
            )

        stock_radius = self.outer_diameter_mm / 2
        pitch_radius = self.bolt_circle_diameter_mm / 2
        hole_radius = self.bolt_hole_diameter_mm / 2
        ligaments = {
            "outer_wall_mm": stock_radius - pitch_radius - hole_radius,
            "center_wall_mm": pitch_radius - self.center_hole_diameter_mm / 2 - hole_radius,
            "neighbor_wall_mm": (
                2 * pitch_radius * math.sin(math.pi / self.hole_count) - self.bolt_hole_diameter_mm
            ),
        }
        for name, ligament in ligaments.items():
            # A small arithmetic guard admits exact decimal boundary values;
            # it does not alter the configured geometric measurement tolerance.
            if ligament < 0.1 - 1e-12:
                raise CadError(
                    "PARAMETER_INVALID",
                    "Holes must be distinct, inside the stock, with at least 0.1 mm material",
                    {"constraint": name, "actual_mm": ligament, "minimum_mm": 0.1},
                )

    @classmethod
    def from_dict(cls, value: Any) -> FlangeSpec:
        payload = _explicit_payload(
            value,
            {field.name for field in fields(cls)},
            {
                "schema_version": cls.SCHEMA_VERSION,
                "units": cls.UNITS,
                "coordinate_system": cls.COORDINATE_SYSTEM,
            },
        )
        return cls(**payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "units": self.UNITS,
            "coordinate_system": self.COORDINATE_SYSTEM,
            **{field.name: getattr(self, field.name) for field in fields(self)},
        }

    def bolt_centers_mm(self) -> list[tuple[float, float]]:
        radius = self.bolt_circle_diameter_mm / 2
        return [
            (
                radius * math.cos(self.start_angle_rad + index * math.tau / self.hole_count),
                radius * math.sin(self.start_angle_rad + index * math.tau / self.hole_count),
            )
            for index in range(self.hole_count)
        ]

    @property
    def expected_volume_mm3(self) -> float:
        area = math.pi * (
            (self.outer_diameter_mm / 2) ** 2
            - (self.center_hole_diameter_mm / 2) ** 2
            - self.hole_count * (self.bolt_hole_diameter_mm / 2) ** 2
        )
        return area * self.thickness_mm


@dataclass(frozen=True)
class PrecisionProfile:
    """Versioned proposed thresholds, not a manufacturing certification."""

    SCHEMA_VERSION: ClassVar[str] = "deep3d.precision/1"

    working_tolerance_mm: float = 0.00001
    max_subshape_tolerance_mm: float = 0.001
    critical_dimension_error_mm: float = 0.001
    step_roundtrip_dimension_error_mm: float = 0.005
    volume_relative_error: float = 0.000001
    measurement_error_budget_mm: float = 0.0001

    def __post_init__(self) -> None:
        for field in fields(self):
            value = _finite_number(field.name, getattr(self, field.name))
            if value <= 0:
                raise CadError(
                    "PARAMETER_INVALID", f"{field.name} must be positive", {"field": field.name}
                )
            object.__setattr__(self, field.name, value)
        if self.working_tolerance_mm > 0.001:
            raise CadError(
                "OUTSIDE_QUALIFIED_ENVELOPE",
                "working_tolerance_mm must be within (0, 0.001] mm",
                {"field": "working_tolerance_mm", "maximum_mm": 0.001},
            )
        if self.working_tolerance_mm > self.max_subshape_tolerance_mm:
            raise CadError(
                "PARAMETER_INVALID", "working tolerance must not exceed maximum subshape tolerance"
            )
        if self.measurement_error_budget_mm >= min(
            self.critical_dimension_error_mm, self.step_roundtrip_dimension_error_mm
        ):
            raise CadError(
                "PARAMETER_INVALID",
                "measurement error budget must be smaller than dimension limits",
            )
        if self.volume_relative_error >= 1:
            raise CadError("PARAMETER_INVALID", "relative volume error must be less than one")

    @classmethod
    def from_dict(cls, value: Any) -> PrecisionProfile:
        return cls(
            **_explicit_payload(
                value,
                {field.name for field in fields(cls)},
                {"schema_version": cls.SCHEMA_VERSION},
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            **{field.name: getattr(self, field.name) for field in fields(self)},
        }
