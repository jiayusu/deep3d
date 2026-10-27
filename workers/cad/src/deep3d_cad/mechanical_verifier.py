"""Reload actual BREP/STEP instances and check an independently frozen mechanical goal."""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import sys
from pathlib import Path
from uuid import UUID

from .contracts import CadError, PrecisionProfile
from .jsonio import read_json, write_json
from .runner import file_digest


def _finite(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise CadError("SCHEMA_INVALID", "Acceptance values must be finite numbers")
    return float(value)


def _node_goal(value):
    if not isinstance(value, dict) or set(value) - {"solid_count", "volume_mm3", "bounds_mm"}:
        raise CadError("SCHEMA_INVALID", "Unexpected node acceptance fields")
    if type(value.get("solid_count")) is not int or not 1 <= value["solid_count"] <= 4096:
        raise CadError("SCHEMA_INVALID", "Acceptance requires 1..4096 solids")
    if value.get("volume_mm3") is None and value.get("bounds_mm") is None:
        raise CadError("SCHEMA_INVALID", "Acceptance requires independent volume or bounds")
    if value.get("volume_mm3") is not None and not 0 < _finite(value["volume_mm3"]):
        raise CadError("SCHEMA_INVALID", "Invalid expected volume")
    if value.get("bounds_mm") is not None:
        bounds = value["bounds_mm"]
        if not isinstance(bounds, list) or len(bounds) != 6:
            raise CadError("SCHEMA_INVALID", "Bounds require xmin,ymin,zmin,xmax,ymax,zmax")
        values = [_finite(v) for v in bounds]
        if any(abs(v) > 10000 for v in values) or any(values[i] >= values[i + 3] for i in range(3)):
            raise CadError("SCHEMA_INVALID", "Expected bounds must enclose positive extents")


def validate_goal(goal: dict) -> dict:
    if (
        not isinstance(goal, dict)
        or goal.get("schema_version") != "deep3d.mechanical-acceptance/1"
        or set(goal) - {"schema_version", "nodes", "assembly", "max_interference_volume_mm3"}
        or not isinstance(goal.get("nodes"), dict)
        or not 1 <= len(goal["nodes"]) <= 64
    ):
        raise CadError("SCHEMA_INVALID", "Expected a bounded mechanical acceptance contract")
    for node, expected in goal["nodes"].items():
        try:
            if str(UUID(node)) != node or UUID(node).int == 0:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise CadError("SCHEMA_INVALID", "Acceptance node requires a canonical UUID") from None
        _node_goal(expected)
    if goal.get("assembly") is not None:
        _node_goal(goal["assembly"])
    if goal.get("max_interference_volume_mm3") is not None:
        if not 0 <= _finite(goal["max_interference_volume_mm3"]) <= 1e12:
            raise CadError("SCHEMA_INVALID", "Invalid allowed interference volume")
    return goal


def source_digest() -> str:
    digest = hashlib.sha256()
    for filename in ("mechanical_verifier.py", "assembly_io.py", "kernel.py", "contracts.py"):
        digest.update(filename.encode())
        digest.update(Path(__file__).with_name(filename).read_bytes())
    return "sha256:" + digest.hexdigest()


def _shapes(shape, kind):
    from OCP.collections import IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher
    from OCP.TopExp import TopExp

    mapped = IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher()
    TopExp.MapShapes_s(shape, kind, mapped)
    return [mapped.FindKey(i) for i in range(1, mapped.Extent() + 1)]


def measurements(shape) -> dict:
    from OCP.Bnd import Bnd_Box
    from OCP.BRep import BRep_Tool
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID, TopAbs_VERTEX
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    solids = _shapes(shape, TopAbs_SOLID)
    volumes = []
    for solid in solids:
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(solid, props)
        volumes.append(props.Mass())
    tolerances = []
    for kind, cast in (
        (TopAbs_VERTEX, TopoDS.Vertex),
        (TopAbs_EDGE, TopoDS.Edge),
        (TopAbs_FACE, TopoDS.Face),
    ):
        tolerances.extend(BRep_Tool.Tolerance_s(cast(item)) for item in _shapes(shape, kind))
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    minimum, maximum = box.CornerMin(), box.CornerMax()
    pure = not any(
        TopExp_Explorer(shape, kind, TopAbs_SOLID).More()
        for kind in (TopAbs_FACE, TopAbs_EDGE, TopAbs_VERTEX)
    )
    return {
        "valid": not shape.IsNull() and BRepCheck_Analyzer(shape, True, False, True).IsValid(),
        "pure_solid_structure": pure,
        "solid_count": len(solids),
        "volume_mm3": sum(volumes),
        "positive_oriented_solids": bool(solids) and all(v > 0 for v in volumes),
        "closed_shells": all(
            BRep_Tool.IsClosed_s(TopoDS.Shell(shell)) for shell in _shapes(shape, TopAbs_SHELL)
        ),
        "max_subshape_tolerance_mm": max(tolerances, default=0),
        "bounds_mm": [minimum.X(), minimum.Y(), minimum.Z(), maximum.X(), maximum.Y(), maximum.Z()],
    }


def verify(directory: Path, stage: str) -> dict:
    from .assembly_io import assembly_manifest, compound, read_assembly
    from .kernel import read_brep

    plan = read_json(directory / "plan.json")
    goal = validate_goal(read_json(directory / "goal.json"))
    precision = PrecisionProfile.from_dict(read_json(directory / "precision.json"))
    if goal != plan["acceptance"] or precision.to_dict() != plan["precision"]:
        raise CadError("INPUT_CHANGED", "Verifier inputs differ from the frozen plan")
    geometry = directory / ("model.brep" if stage == "native" else "model.step")
    before = file_digest(geometry)
    node_hashes = {}
    assembly_hash = None
    if stage == "native":
        assembly_hash = file_digest(directory / "assembly.json")
        nodes = {}
        for node in goal["nodes"]:
            path = directory / "nodes" / f"{node}.brep"
            node_hashes[node] = file_digest(path)
            nodes[node] = read_brep(path)
        aggregate = read_brep(geometry)
        meta = read_json(directory / "assembly.json")
        expected_meta = assembly_manifest(plan)
        if meta != expected_meta:
            raise CadError("ASSEMBLY_INVALID", "Assembly metadata differs from frozen scene")
    else:
        nodes, metadata = read_assembly(geometry)
        expected_meta = assembly_manifest(plan)
        expected_nodes = {
            node: {"name": item["name"], "parent": item["parent"]}
            for node, item in plan["scene"].items()
            if node in metadata
        }
        for node, item in expected_meta["instances"].items():
            expected_nodes.setdefault(node, {})["part_id"] = item["part_id"]
        if metadata != expected_nodes:
            raise CadError(
                "ASSEMBLY_NAMES_MISSING", "STEP instance hierarchy or part identities differ"
            )
        aggregate = compound(nodes.values())
    checks = []
    observed = {}

    def check(identifier, passed, actual, wanted):
        checks.append(
            {
                "id": identifier,
                "status": "pass" if passed else "fail",
                "observed": actual,
                "expected": wanted,
            }
        )

    check(
        "assembly.node_set", set(nodes) == set(goal["nodes"]), sorted(nodes), sorted(goal["nodes"])
    )
    limit = precision.critical_dimension_error_mm
    if stage == "step":
        limit = min(limit, precision.step_roundtrip_dimension_error_mm)

    def inspect(node, shape, expected):
        values = measurements(shape)
        observed[node] = values
        for name in ("valid", "positive_oriented_solids", "closed_shells", "pure_solid_structure"):
            check(f"{node}.{name}", values[name], values[name], True)
        tolerance = values["max_subshape_tolerance_mm"]
        check(
            f"{node}.subshape_tolerance",
            tolerance <= precision.max_subshape_tolerance_mm,
            tolerance,
            precision.max_subshape_tolerance_mm,
        )
        if expected is None:
            return values
        check(
            f"{node}.solid_count",
            values["solid_count"] == expected["solid_count"],
            values["solid_count"],
            expected["solid_count"],
        )
        if expected.get("volume_mm3") is not None:
            value = expected["volume_mm3"]
            error = abs(values["volume_mm3"] - value) / value
            check(
                f"{node}.volume",
                error <= precision.volume_relative_error,
                values["volume_mm3"],
                value,
            )
        if expected.get("bounds_mm") is not None:
            for axis, (actual, wanted) in enumerate(
                zip(values["bounds_mm"], expected["bounds_mm"])
            ):
                check(
                    f"{node}.bounds.{axis}",
                    abs(actual - wanted) + precision.measurement_error_budget_mm <= limit,
                    actual,
                    wanted,
                )
        return values

    parts = [inspect(node, nodes[node], goal["nodes"][node]) for node in sorted(nodes)]
    combined = inspect("assembly", aggregate, goal.get("assembly"))
    total_volume = sum(part["volume_mm3"] for part in parts)
    check(
        "assembly.instance_count",
        combined["solid_count"] == sum(p["solid_count"] for p in parts),
        combined["solid_count"],
        sum(p["solid_count"] for p in parts),
    )
    check(
        "assembly.volume_conservation",
        abs(combined["volume_mm3"] - total_volume)
        <= max(total_volume * precision.volume_relative_error, 1e-9),
        combined["volume_mm3"],
        total_volume,
    )
    reference = measurements(compound(nodes.values()))
    check(
        "assembly.bounds_conservation",
        all(
            abs(a - b) + precision.measurement_error_budget_mm <= limit
            for a, b in zip(combined["bounds_mm"], reference["bounds_mm"])
        ),
        combined["bounds_mm"],
        reference["bounds_mm"],
    )
    if goal.get("max_interference_volume_mm3") is not None:
        from OCP.BOPAlgo import BOPAlgo_BOP, BOPAlgo_COMMON
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps

        ordered = sorted(nodes)
        for i, first in enumerate(ordered):
            for second in ordered[i + 1 :]:
                operation = BOPAlgo_BOP()
                operation.SetOperation(BOPAlgo_COMMON)
                operation.AddArgument(nodes[first])
                operation.AddTool(nodes[second])
                operation.SetFuzzyValue(precision.working_tolerance_mm)
                operation.SetNonDestructive(True)
                operation.Perform()
                if operation.HasErrors() or operation.HasWarnings():
                    raise CadError("INTERFERENCE_INCONCLUSIVE", "Cannot compute part intersection")
                props = GProp_GProps()
                BRepGProp.VolumeProperties_s(operation.Shape(), props)
                overlap = abs(props.Mass())
                allowed = goal["max_interference_volume_mm3"]
                check(f"interference.{first}.{second}", overlap <= allowed + 1e-9, overlap, allowed)
    if file_digest(geometry) != before:
        raise CadError("ARTIFACT_CHANGED", "Geometry changed during independent verification")
    for node, digest in node_hashes.items():
        if file_digest(directory / "nodes" / f"{node}.brep") != digest:
            raise CadError("ARTIFACT_CHANGED", "Node BREP changed during verification")
    if assembly_hash is not None and file_digest(directory / "assembly.json") != assembly_hash:
        raise CadError("ARTIFACT_CHANGED", "Assembly metadata changed during verification")
    return {
        "schema_version": "deep3d.mechanical-verification/1",
        "stage": stage,
        "status": "pass" if all(c["status"] == "pass" for c in checks) else "fail",
        "expected_spec": goal,
        "precision_profile": precision.to_dict(),
        "checks": checks,
        "measurements": observed,
        "worker_pid": os.getpid(),
        "input_artifact_sha256": before,
        "input_node_artifact_sha256": node_hashes,
        "input_assembly_artifact_sha256": assembly_hash,
        "validator_id": "deep3d.mechanical@1",
        "validator_source_sha256": source_digest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--stage", choices=["native", "step"], required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = verify(args.directory, args.stage)
    except CadError as exc:
        report = {"status": "error", "error": exc.to_dict()}
    except Exception as exc:
        report = {"status": "error", "error": {"code": "VERIFIER_ERROR", "message": str(exc)}}
    write_json(args.report, report)
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    sys.exit(main())
