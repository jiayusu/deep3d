"""Execute a versioned, typed Feature DAG using a fixed OCCT operation registry.

The caller owns the specification and publication policy. This worker only
materializes that specification and obtains independent fixture checks. It
does not evaluate arbitrary Python, infer missing references, or move a head.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from .contracts import CadError, FlangeSpec, PrecisionProfile
from .jsonio import MAX_JSON_BYTES, read_json, write_json
from .runner import file_digest, run_worker
from .worker import environment

MAX_FEATURES = 256
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
PLAN_FIELDS = {
    "schema_version",
    "project_id",
    "revision_id",
    "units",
    "coordinate_system",
    "features",
    "outputs",
    "acceptance",
    "precision",
}
FEATURE_FIELDS = {"id", "name", "op", "inputs", "parameters"}
REGISTRY = {
    "profile.circle@1": ({}, {"radius_mm", "center_x_mm", "center_y_mm"}, "profile"),
    "solid.extrude@1": ({"profile": "profile"}, {"distance_mm"}, "solid"),
    "solid.cut@1": ({"base": "solid", "tool": "solid"}, set(), "solid"),
}


def _object(value: Any, fields: set[str] | None, label: str) -> dict:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise CadError("SCHEMA_INVALID", f"{label} must be an object with string keys")
    if fields is not None and set(value) != fields:
        raise CadError(
            "SCHEMA_INVALID",
            f"{label} fields do not match the contract",
            {
                "missing": sorted(fields - set(value)),
                "unknown": sorted(set(value) - fields),
            },
        )
    return value


def _uuid(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise CadError("SCHEMA_INVALID", f"{label} must be a canonical UUID string")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise CadError("SCHEMA_INVALID", f"{label} is not a UUID") from exc
    if str(parsed) != value:
        raise CadError("SCHEMA_INVALID", f"{label} must be a lowercase hyphenated UUID")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise CadError("PARAMETER_INVALID", f"{label} must be a finite f64 number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise CadError("PARAMETER_INVALID", f"{label} is outside finite f64") from exc
    if not math.isfinite(number):
        raise CadError("PARAMETER_INVALID", f"{label} must be finite")
    return number


def _snapshot(plan: Any) -> dict:
    """Copy input data before validation/execution, with a bounded serialization."""
    try:
        encoded = json.dumps(plan, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise CadError("SCHEMA_INVALID", "Execution plan is not finite UTF-8 JSON") from exc
    if len(encoded) > MAX_JSON_BYTES:
        raise CadError("INPUT_TOO_LARGE", "Execution plan exceeds 1 MiB")
    return json.loads(encoded)


def validate_plan(plan: Any) -> tuple[dict, FlangeSpec, PrecisionProfile]:
    """Preflight every operation and edge before importing the CAD kernel."""
    plan = _object(_snapshot(plan), PLAN_FIELDS, "ExecutionPlan")
    if plan["schema_version"] != "deep3d.execution-plan/1":
        raise CadError("SCHEMA_UNSUPPORTED", "Unsupported execution plan schema")
    _uuid(plan["project_id"], "project_id")
    _uuid(plan["revision_id"], "revision_id")
    if plan["units"] != "mm" or plan["coordinate_system"] != "right_handed_z_up":
        raise CadError("SCHEMA_INVALID", "Execution plan requires mm and right_handed_z_up")
    goal = FlangeSpec.from_dict(plan["acceptance"])
    precision = PrecisionProfile.from_dict(plan["precision"])
    if not isinstance(plan["features"], list) or not 1 <= len(plan["features"]) <= MAX_FEATURES:
        raise CadError("RESOURCE_LIMIT", "Execution plan requires 1..256 features")

    types: dict[str, str] = {}
    for index, raw_feature in enumerate(plan["features"]):
        feature = _object(raw_feature, FEATURE_FIELDS, f"Feature[{index}]")
        feature_id = _uuid(feature["id"], "feature.id")
        if feature_id in types:
            raise CadError("DUPLICATE_ID", "Feature ID is duplicated", {"feature_id": feature_id})
        if not isinstance(feature["name"], str) or not 1 <= len(feature["name"]) <= 256:
            raise CadError("SCHEMA_INVALID", "Feature name must contain 1..256 characters")
        op = feature["op"]
        if not isinstance(op, str) or op not in REGISTRY:
            raise CadError("CAPABILITY_UNAVAILABLE", "Unsupported feature operation")
        input_types, parameter_names, output_type = REGISTRY[op]
        inputs = _object(feature["inputs"], set(input_types), "Feature.inputs")
        parameters = _object(feature["parameters"], parameter_names, "Feature.parameters")
        for port, wanted_type in input_types.items():
            source = _uuid(inputs[port], f"inputs.{port}")
            if source not in types:
                raise CadError(
                    "DEPENDENCY_NOT_READY",
                    "Missing, forward or cyclic feature reference",
                    {
                        "feature_id": feature_id,
                        "port": port,
                        "source_id": source,
                    },
                )
            if types[source] != wanted_type:
                raise CadError(
                    "TYPE_MISMATCH",
                    "Feature input has the wrong geometry type",
                    {
                        "feature_id": feature_id,
                        "port": port,
                    },
                )
        for key, value in parameters.items():
            parameters[key] = _number(value, key)
        if op == "profile.circle@1":
            if not 0.05 <= parameters["radius_mm"] <= 500:
                raise CadError("OUTSIDE_QUALIFIED_ENVELOPE", "Circle radius requires 0.05..500 mm")
            if any(abs(parameters[key]) > 1000 for key in ("center_x_mm", "center_y_mm")):
                raise CadError(
                    "OUTSIDE_QUALIFIED_ENVELOPE", "Profile center exceeds fixture envelope"
                )
        elif op == "solid.extrude@1" and not 1 <= parameters["distance_mm"] <= 1000:
            raise CadError("OUTSIDE_QUALIFIED_ENVELOPE", "Extrusion distance requires 1..1000 mm")
        types[feature_id] = output_type

    outputs = _object(plan["outputs"], None, "ExecutionPlan.outputs")
    if len(outputs) != 1:
        raise CadError(
            "CAPABILITY_UNAVAILABLE", "Initial evaluator supports exactly one flange output"
        )
    for node_id, feature_id in outputs.items():
        _uuid(node_id, "output.node_id")
        _uuid(feature_id, "output.feature_id")
        if feature_id not in types:
            raise CadError("DEPENDENCY_NOT_READY", "Output feature does not exist")
        if types[feature_id] != "solid":
            raise CadError("TYPE_MISMATCH", "Scene output must be a solid")
    return plan, goal, precision


def _artifacts(directory: Path) -> list[dict]:
    result = []
    total = 0
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise CadError("ARTIFACT_INVALID", "Worker output cannot contain symbolic links")
        if not path.is_file() or path.name == "result.json":
            continue
        size = path.stat().st_size
        total += size
        if size > MAX_ARTIFACT_BYTES or total > MAX_TOTAL_BYTES:
            raise CadError("RESOURCE_LIMIT", "Worker artifacts exceed the declared storage budget")
        result.append(
            {
                "path": path.relative_to(directory).as_posix(),
                "sha256": file_digest(path),
                "size_bytes": size,
            }
        )
    return result


def _circle(parameters: dict):
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeEdge,
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakeWire,
    )
    from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pnt

    circle = gp_Circ(
        gp_Ax2(gp_Pnt(parameters["center_x_mm"], parameters["center_y_mm"], 0), gp_Dir(0, 0, 1)),
        parameters["radius_mm"],
    )
    edge = BRepBuilderAPI_MakeEdge(circle)
    wire = BRepBuilderAPI_MakeWire(edge.Edge())
    face = BRepBuilderAPI_MakeFace(wire.Wire())
    if not all((edge.IsDone(), wire.IsDone(), face.IsDone())):
        raise CadError("GEOMETRY_KERNEL_FAILURE", "OCCT circle profile construction failed")
    return face.Face()


def evaluate_plan(plan: dict, directory: Path, *, timeout: float = 120) -> dict:
    """Materialize an input snapshot into a new directory, then verify reloaded B-Reps."""
    plan, goal, precision = validate_plan(plan)
    if isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 600:
        raise CadError("PARAMETER_INVALID", "Worker timeout requires (0, 600] seconds")
    directory = directory.resolve()
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise CadError(
            "OUTPUT_EXISTS", "Project evaluation requires a new output directory"
        ) from exc

    result: dict = {
        "schema_version": "deep3d.project-evaluation/1",
        "status": "error",
        "project_id": plan["project_id"],
        "revision_id": plan["revision_id"],
        "feature_outputs": {},
        "node_outputs": {},
        "stages": {},
        "evaluator_source_sha256": "sha256:"
        + hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    operations = []
    try:
        from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
        from OCP.gp import gp_Vec

        from .kernel import _cut, _require_shape, export_step, write_brep

        result["environment"] = environment()
        result["environment"]["evaluator_source_sha256"] = result["evaluator_source_sha256"]
        write_json(directory / "plan.json", plan)
        write_json(directory / "goal.json", goal.to_dict())
        write_json(directory / "precision.json", precision.to_dict())
        shapes = {}
        feature_directory = directory / "features"
        feature_directory.mkdir()
        for feature in plan["features"]:
            parameters, inputs = feature["parameters"], feature["inputs"]
            op = feature["op"]
            diagnostic = {"feature_id": feature["id"], "op": op, "parameters": parameters}
            if op == "profile.circle@1":
                shape = _circle(parameters)
            elif op == "solid.extrude@1":
                builder = BRepPrimAPI_MakePrism(
                    shapes[inputs["profile"]], gp_Vec(0, 0, parameters["distance_mm"]), True, True
                )
                shape = builder.Shape()
                if not builder.IsDone():
                    raise CadError("GEOMETRY_KERNEL_FAILURE", "OCCT extrusion failed")
            else:
                shape, cut_report = _cut(
                    shapes[inputs["base"]],
                    shapes[inputs["tool"]],
                    feature["name"],
                    working_tolerance_mm=precision.working_tolerance_mm,
                )
                diagnostic["kernel"] = cut_report
            _require_shape(shape, operation=feature["id"])
            shapes[feature["id"]] = shape
            path = feature_directory / f"{feature['id']}.brep"
            write_brep(shape, path)
            if path.stat().st_size > MAX_ARTIFACT_BYTES:
                raise CadError("RESOURCE_LIMIT", "Feature BREP exceeds 64 MiB")
            result["feature_outputs"][feature["id"]] = file_digest(path)
            operations.append(diagnostic)

        node_id, final_feature = next(iter(plan["outputs"].items()))
        final_shape = shapes[final_feature]
        write_brep(final_shape, directory / "flange.brep")
        result["node_outputs"][node_id] = file_digest(directory / "flange.brep")
        exchange = export_step(
            final_shape,
            directory / "flange.step",
            working_tolerance_mm=precision.working_tolerance_mm,
        )
        build = {
            "schema_version": "deep3d.feature-build/1",
            "status": "pass",
            "project_id": plan["project_id"],
            "revision_id": plan["revision_id"],
            "worker_pid": os.getpid(),
            "environment": result["environment"],
            "operations": operations,
            "exchange": exchange,
        }
        write_json(directory / "build.json", build)
        result["stages"]["build"] = build
        for action in ("verify-brep", "verify-step"):
            result["stages"][action] = run_worker(action, directory, timeout=timeout)
        validator = result["stages"]["verify-brep"]["validator_source_sha256"]
        if validator != result["stages"]["verify-step"]["validator_source_sha256"]:
            raise CadError("ENGINE_CHANGED", "Verifier build changed between checks")
        result["environment"]["verifier_source_sha256"] = validator
        result["status"] = (
            "pass" if all(s["status"] == "pass" for s in result["stages"].values()) else "fail"
        )
        result["artifacts"] = _artifacts(directory)
    except CadError as exc:
        result["error"] = exc.to_dict()
    except Exception as exc:
        result["error"] = {
            "code": "WORKER_ERROR",
            "message": str(exc),
            "exception": type(exc).__name__,
        }
    if result["status"] == "error":
        # Partial artifacts are diagnostic only; Rust must not accept this result.
        write_json(
            directory / "build-failure.json",
            {
                "status": "error",
                "operations": operations,
                "error": result["error"],
            },
        )
        result["artifacts"] = []
    write_json(directory / "result.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args(argv)
    try:
        result = evaluate_plan(read_json(args.plan), args.out, timeout=args.timeout)
        print(json.dumps({"status": result["status"], "output": str(args.out.resolve())}))
        return 0 if result["status"] == "pass" else 2
    except CadError as exc:
        print(json.dumps({"status": "error", "error": exc.to_dict()}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
