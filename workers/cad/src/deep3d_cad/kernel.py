"""Small, explicit OCCT adapter for the M0 flange qualification.

Lengths are millimetres in the project convention.  Topology indices in
operation history are local diagnostics, never persistent semantic names.
The caller must execute this adapter in a dedicated process: OCCT exchange
configuration is process global.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from OCP.BOPAlgo import BOPAlgo_BOP, BOPAlgo_CUT
from OCP.BRep import BRep_Builder
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
from OCP.BRepTools import BRepTools
from OCP.collections import (
    IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher,
    Sequence_TCollection_AsciiString,
)
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.IFSelect import IFSelect_RetDone
from OCP.Interface import Interface_Static
from OCP.Message import Message_Fail, Message_Warning
from OCP.STEPControl import (
    STEPControl_ManifoldSolidBrep,
    STEPControl_Reader,
    STEPControl_Writer,
)
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID, TopAbs_VERTEX
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS_Shape
from OCP.TopTools import TopTools_FormatVersion_VERSION_3

from .contracts import CadError, FlangeSpec

WORKING_TOLERANCE_MM = 1e-5


@dataclass(frozen=True)
class BuildResult:
    shape: TopoDS_Shape
    operations: list[dict[str, Any]]


def _require_shape(shape: TopoDS_Shape, *, operation: str) -> None:
    if shape.IsNull():
        raise CadError("EMPTY_GEOMETRY", "Kernel returned a null shape", {"operation": operation})
    if not BRepCheck_Analyzer(shape, True, False, True).IsValid():
        raise CadError(
            "GEOMETRY_INVALID", "Kernel shape fails B-Rep validity", {"operation": operation}
        )


def _map(shape: TopoDS_Shape, kind: Any | None = None) -> Any:
    result = IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher()
    if kind is None:
        TopExp.MapShapes_s(shape, result)
    else:
        TopExp.MapShapes_s(shape, kind, result)
    return result


def _history(operation: BOPAlgo_BOP, argument: TopoDS_Shape, tool: TopoDS_Shape) -> dict[str, Any]:
    """Record actual kernel relations using operation-local shape indices."""
    output_map = _map(operation.Shape())
    rows: list[dict[str, Any]] = []
    totals = {"generated": 0, "modified": 0, "deleted": 0, "retained": 0}
    for role, source in (("argument", argument), ("tool", tool)):
        for kind, name in ((TopAbs_VERTEX, "vertex"), (TopAbs_EDGE, "edge"), (TopAbs_FACE, "face")):
            source_map = _map(source, kind)
            for index in range(1, source_map.Extent() + 1):
                subshape = source_map.FindKey(index)
                generated = [output_map.FindIndex(s) for s in operation.Generated(subshape)]
                modified = [output_map.FindIndex(s) for s in operation.Modified(subshape)]
                deleted = bool(operation.IsDeleted(subshape))
                retained = output_map.FindIndex(subshape)
                rows.append(
                    {
                        "source_role": role,
                        "source_type": name,
                        "source_local_index": index,
                        "generated_output_local_indices": generated,
                        "modified_output_local_indices": modified,
                        "deleted": deleted,
                        "retained_output_local_index": retained or None,
                    }
                )
                totals["generated"] += len(generated)
                totals["modified"] += len(modified)
                totals["deleted"] += int(deleted)
                totals["retained"] += int(bool(retained))
    return {
        "available": bool(operation.HasHistory()),
        "scope": "operation-local vertices/edges/faces; not stable naming",
        "counts": totals,
        "relations": rows,
    }


def _alerts(operation: BOPAlgo_BOP, grade: Any) -> list[str]:
    return [alert.DynamicType().Name() for alert in operation.GetReport().GetAlerts(grade)]


def _working_tolerance(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CadError("PARAMETER_INVALID", "working_tolerance_mm must be a finite number")
    value = float(value)
    if not math.isfinite(value) or not 0 < value <= 0.001:
        raise CadError(
            "OUTSIDE_QUALIFIED_ENVELOPE", "working_tolerance_mm must be within (0, 0.001] mm"
        )
    return value


def _cut(
    argument: TopoDS_Shape,
    tool: TopoDS_Shape,
    name: str,
    *,
    working_tolerance_mm: float = WORKING_TOLERANCE_MM,
) -> tuple[TopoDS_Shape, dict[str, Any]]:
    """Use BOPAlgo directly because OCP8 Cut's wrapper omits report methods."""
    _require_shape(argument, operation=f"{name}.argument")
    _require_shape(tool, operation=f"{name}.tool")
    operation = BOPAlgo_BOP()
    operation.SetOperation(BOPAlgo_CUT)
    operation.SetNonDestructive(True)
    operation.SetRunParallel(False)
    operation.SetToFillHistory(True)
    operation.SetFuzzyValue(_working_tolerance(working_tolerance_mm))
    operation.AddArgument(argument)
    operation.AddTool(tool)
    operation.Perform()
    diagnostics = {
        "operation": name,
        "type": "solid.cut@1",
        "non_destructive": bool(operation.NonDestructive()),
        "parallel": False,
        "fuzzy_tolerance_mm": float(operation.FuzzyValue()),
        "errors": _alerts(operation, Message_Fail),
        "warnings": _alerts(operation, Message_Warning),
    }
    if operation.HasErrors():
        raise CadError("GEOMETRY_KERNEL_FAILURE", "OCCT Boolean failed", diagnostics)
    # Qualification is fail-closed. A warning needs a separately reviewed policy.
    if operation.HasWarnings():
        raise CadError(
            "GEOMETRY_KERNEL_WARNING", "OCCT Boolean produced an unqualified warning", diagnostics
        )
    result = operation.Shape()
    _require_shape(result, operation=name)
    solid_count = _map(result, TopAbs_SOLID).Extent()
    if solid_count == 0:
        raise CadError(
            "EMPTY_GEOMETRY",
            "Boolean has a valid empty result, forbidden by flange contract",
            diagnostics,
        )
    if solid_count != 1:
        raise CadError(
            "UNEXPECTED_SOLID_COUNT",
            "Flange Boolean must preserve one solid",
            {
                **diagnostics,
                "solid_count": solid_count,
            },
        )
    diagnostics["history"] = _history(operation, argument, tool)
    return result, diagnostics


def _cylinder(radius: float, height: float, x: float = 0.0, y: float = 0.0) -> TopoDS_Shape:
    builder = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, 0.0), gp_Dir(0, 0, 1)), radius, height)
    shape = builder.Shape()
    if not builder.IsDone():
        raise CadError("GEOMETRY_KERNEL_FAILURE", "OCCT cylinder construction failed")
    _require_shape(shape, operation="cylinder")
    return shape


def build_flange(
    spec: FlangeSpec, *, working_tolerance_mm: float = WORKING_TOLERANCE_MM
) -> BuildResult:
    """Construct a single planar, cylindrical flange from validated dimensions."""
    working_tolerance_mm = _working_tolerance(working_tolerance_mm)
    try:
        shape = _cylinder(spec.outer_diameter_mm / 2, spec.thickness_mm)
        operations: list[dict[str, Any]] = [
            {
                "operation": "stock",
                "type": "solid.cylinder@1",
                "radius_mm": spec.outer_diameter_mm / 2,
                "height_mm": spec.thickness_mm,
                "errors": [],
                "warnings": [],
            }
        ]
        center = _cylinder(spec.center_hole_diameter_mm / 2, spec.thickness_mm)
        shape, diagnostic = _cut(
            shape, center, "center_hole", working_tolerance_mm=working_tolerance_mm
        )
        operations.append(diagnostic)
        for index, (x, y) in enumerate(spec.bolt_centers_mm()):
            tool = _cylinder(spec.bolt_hole_diameter_mm / 2, spec.thickness_mm, x, y)
            shape, diagnostic = _cut(
                shape, tool, f"bolt_hole_{index}", working_tolerance_mm=working_tolerance_mm
            )
            operations.append(diagnostic)
        return BuildResult(shape, operations)
    except CadError:
        raise
    except Exception as exc:
        raise CadError(
            "GEOMETRY_KERNEL_FAILURE",
            "OCCT flange construction raised an exception",
            {
                "exception": type(exc).__name__,
                "detail": str(exc),
            },
        ) from exc


def _brep_bytes(shape: TopoDS_Shape) -> bytes:
    stream = io.BytesIO()
    BRepTools.Write_s(shape, stream, False, False, TopTools_FormatVersion_VERSION_3)
    return stream.getvalue()


def write_brep(shape: TopoDS_Shape, path: str | Path) -> None:
    _require_shape(shape, operation="brep.write")
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not BRepTools.Write_s(
            shape, str(target), False, False, TopTools_FormatVersion_VERSION_3
        ):
            raise CadError(
                "ARTIFACT_WRITE_FAILED", "OCCT BREP writer failed", {"path": str(target)}
            )
    except CadError:
        raise
    except Exception as exc:
        raise CadError(
            "ARTIFACT_WRITE_FAILED", "Could not write BREP", {"detail": str(exc)}
        ) from exc


def read_brep(path: str | Path) -> TopoDS_Shape:
    source = Path(path)
    if not source.is_file():
        raise CadError("ARTIFACT_NOT_FOUND", "BREP file does not exist", {"path": str(source)})
    try:
        shape = TopoDS_Shape()
        if not BRepTools.Read_s(shape, str(source), BRep_Builder()):
            raise CadError(
                "ARTIFACT_READ_FAILED", "OCCT could not read BREP", {"path": str(source)}
            )
        _require_shape(shape, operation="brep.read")
        return shape
    except CadError:
        raise
    except Exception as exc:
        raise CadError("ARTIFACT_READ_FAILED", "Could not read BREP", {"detail": str(exc)}) from exc


def _transfer_checks(checks: Any) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {"errors": [], "warnings": []}
    checks.Start()
    while checks.More():
        check = checks.Value()
        result["errors"].extend(check.CFail(i) for i in range(1, check.NbFails() + 1))
        result["warnings"].extend(check.CWarning(i) for i in range(1, check.NbWarnings() + 1))
        checks.Next()
    return result


def export_step(
    shape: TopoDS_Shape, path: str | Path, *, working_tolerance_mm: float = WORKING_TOLERANCE_MM
) -> dict[str, Any]:
    """Write exact solids in a narrowly declared AP214IS/mm exchange profile."""
    working_tolerance_mm = _working_tolerance(working_tolerance_mm)
    _require_shape(shape, operation="step.export")
    # Constructor initializes exchange parameter definitions. Each job is serial.
    STEPControl_Writer()
    keys = ("write.step.schema", "write.step.unit")
    old = {key: Interface_Static.CVal_s(key) for key in keys}
    try:
        for key, value in zip(keys, ("AP214IS", "MM")):
            if not Interface_Static.SetCVal_s(key, value):
                raise CadError(
                    "CAPABILITY_UNAVAILABLE",
                    "OCCT rejected STEP exchange configuration",
                    {"key": key},
                )
        writer = STEPControl_Writer()
        writer.SetTolerance(working_tolerance_mm)
        status = writer.Transfer(shape, STEPControl_ManifoldSolidBrep)
        checks = _transfer_checks(writer.WS().TransferWriter().CheckList())
        if status != IFSelect_RetDone or checks["errors"] or checks["warnings"]:
            raise CadError(
                "STEP_TRANSFER_FAILED",
                "OCCT STEP transfer was not clean",
                {
                    "status": str(status),
                    **checks,
                },
            )
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        status = writer.Write(str(target))
        if status != IFSelect_RetDone or not target.is_file() or not target.stat().st_size:
            raise CadError(
                "ARTIFACT_WRITE_FAILED", "OCCT STEP writer failed", {"status": str(status)}
            )
        return {
            "profile": "deep3d.step.ap214-mm@1",
            "schema": "AP214IS",
            "unit": "mm",
            "representation": "manifold_solid_brep",
            "writer_tolerance_mm": working_tolerance_mm,
            "transfer_status": str(IFSelect_RetDone),
            **checks,
        }
    except CadError:
        raise
    except Exception as exc:
        raise CadError(
            "STEP_EXPORT_FAILED", "STEP export raised an exception", {"detail": str(exc)}
        ) from exc
    finally:
        for key, value in old.items():
            Interface_Static.SetCVal_s(key, value)


def import_step(path: str | Path) -> TopoDS_Shape:
    """Read the initial, explicitly millimetre STEP profile without unit guessing."""
    source = Path(path)
    if not source.is_file():
        raise CadError("ARTIFACT_NOT_FOUND", "STEP file does not exist", {"path": str(source)})
    try:
        reader = STEPControl_Reader()
        status = reader.ReadFile(str(source))
        if status != IFSelect_RetDone:
            raise CadError("STEP_READ_FAILED", "OCCT could not parse STEP", {"status": str(status)})
        lengths = Sequence_TCollection_AsciiString()
        angles = Sequence_TCollection_AsciiString()
        solid_angles = Sequence_TCollection_AsciiString()
        reader.FileUnits(lengths, angles, solid_angles)
        units = [lengths.Value(i).ToCString() for i in range(1, lengths.Length() + 1)]
        if not units or any(u.lower() not in {"millimetre", "millimeter", "mm"} for u in units):
            raise CadError(
                "STEP_UNIT_UNSUPPORTED",
                "Initial STEP profile requires explicit millimetre units",
                {"units": units},
            )
        reader.SetSystemLengthUnit(1.0)
        roots = reader.NbRootsForTransfer()
        transferred = reader.TransferRoots()
        checks = _transfer_checks(reader.WS().TransferReader().LastCheckList())
        if roots < 1 or transferred != roots or checks["errors"] or checks["warnings"]:
            raise CadError(
                "STEP_TRANSFER_FAILED",
                "STEP root transfer was incomplete or reported issues",
                {
                    "roots": roots,
                    "transferred": transferred,
                    **checks,
                },
            )
        shape = reader.OneShape()
        _require_shape(shape, operation="step.import")
        return shape
    except CadError:
        raise
    except Exception as exc:
        raise CadError(
            "STEP_IMPORT_FAILED", "STEP import raised an exception", {"detail": str(exc)}
        ) from exc
