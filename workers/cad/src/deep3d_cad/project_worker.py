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
import shutil
import subprocess
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


def _snapshot(plan: Any) -> dict:
    """Copy input data before validation/execution, with a bounded serialization."""
    try:
        encoded = json.dumps(plan, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise CadError("SCHEMA_INVALID", "Execution plan is not finite UTF-8 JSON") from exc
    if len(encoded) > MAX_JSON_BYTES:
        raise CadError("INPUT_TOO_LARGE", "Execution plan exceeds 1 MiB")
    return json.loads(encoded)


def validate_plan(plan: Any) -> tuple[dict, Any, PrecisionProfile]:
    """Preflight every operation and edge before importing the CAD kernel."""
    plan = _snapshot(plan)
    plan = _object(plan, PLAN_FIELDS | ({"scene"} if "scene" in plan else set()), "ExecutionPlan")
    if plan["schema_version"] != "deep3d.execution-plan/1":
        raise CadError("SCHEMA_UNSUPPORTED", "Unsupported execution plan schema")
    _uuid(plan["project_id"], "project_id")
    _uuid(plan["revision_id"], "revision_id")
    if plan["units"] != "mm" or plan["coordinate_system"] != "right_handed_z_up":
        raise CadError("SCHEMA_INVALID", "Execution plan requires mm and right_handed_z_up")
    acceptance = _object(plan["acceptance"], None, "ExecutionPlan.acceptance")
    mechanical = acceptance.get("schema_version") == "deep3d.mechanical-acceptance/1"
    if mechanical:
        from .mechanical_verifier import validate_goal

        goal = validate_goal(plan["acceptance"])
    else:
        goal = FlangeSpec.from_dict(plan["acceptance"])
    precision = PrecisionProfile.from_dict(plan["precision"])
    if not isinstance(plan["features"], list) or not 1 <= len(plan["features"]) <= MAX_FEATURES:
        raise CadError("RESOURCE_LIMIT", "Execution plan requires 1..256 features")

    from .features import validate_feature

    types: dict[str, str] = {}
    multiplicities: dict[str, int] = {}
    for index, raw_feature in enumerate(plan["features"]):
        fields = FEATURE_FIELDS | (
            {"data"} if isinstance(raw_feature, dict) and "data" in raw_feature else set()
        )
        feature = _object(raw_feature, fields, f"Feature[{index}]")
        feature_id = _uuid(feature["id"], "feature.id")
        if feature_id in types:
            raise CadError("DUPLICATE_ID", "Feature ID is duplicated", {"feature_id": feature_id})
        if not isinstance(feature["name"], str) or not 1 <= len(feature["name"]) <= 256:
            raise CadError("SCHEMA_INVALID", "Feature name must contain 1..256 characters")
        op = feature["op"]
        if op == "profile.sketch@1":
            from .sketch import validate_sketch

            _object(feature["inputs"], set(), "Sketch.inputs")
            _object(feature["parameters"], set(), "Sketch.parameters")
            data = _object(feature.get("data"), {"sketch"}, "Sketch.data")
            validate_sketch(data["sketch"])
            output_type = "profile"
        else:
            output_type = validate_feature(feature, types)
        count = 1
        inputs = feature["inputs"]
        if "shape" in inputs:
            count = multiplicities[inputs["shape"]]
        elif "base" in inputs:
            count = multiplicities[inputs["base"]]
            if op == "solid.fuse@1":
                count += multiplicities[inputs["tool"]]
        elif op == "assembly.compound@1":
            count = sum(multiplicities[v] for v in inputs.values())
        if op in {"shape.linear_pattern@1", "shape.circular_pattern@1"}:
            count *= feature["data"]["count"]
        if count > 4096:
            raise CadError("RESOURCE_LIMIT", "Nested instances exceed 4096 solids")
        multiplicities[feature_id] = count
        types[feature_id] = output_type

    outputs = _object(plan["outputs"], None, "ExecutionPlan.outputs")
    if not outputs or len(outputs) > 64 or (not mechanical and len(outputs) != 1):
        raise CadError(
            "CAPABILITY_UNAVAILABLE", "Flange requires one output; mechanical supports 1..64"
        )
    for node_id, feature_id in outputs.items():
        _uuid(node_id, "output.node_id")
        _uuid(feature_id, "output.feature_id")
        if feature_id not in types:
            raise CadError("DEPENDENCY_NOT_READY", "Output feature does not exist")
        if types[feature_id] != "solid":
            raise CadError("TYPE_MISMATCH", "Scene output must be a solid")
    if mechanical:
        if set(outputs) != set(goal["nodes"]):
            raise CadError("ACCEPTANCE_INVALID", "Acceptance must exactly cover scene outputs")
        scene = _object(plan.get("scene"), None, "ExecutionPlan.scene")
        if any(
            node not in scene or scene[node].get("feature") != feature
            for node, feature in outputs.items()
        ):
            raise CadError("ASSEMBLY_INVALID", "Output has no exact scene binding")
        roots = []
        for node, item in scene.items():
            _uuid(node, "scene.node")
            item = _object(item, {"id", "name", "parent", "feature"}, "SceneNode")
            if item["id"] != node or not isinstance(item["name"], str) or not item["name"]:
                raise CadError("SCHEMA_INVALID", "Invalid scene node")
            if item["parent"] is None:
                roots.append(node)
            elif item["parent"] not in scene:
                raise CadError("DEPENDENCY_NOT_READY", "Scene parent is missing")
            if item["feature"] is not None and outputs.get(node) != item["feature"]:
                raise CadError("ASSEMBLY_INVALID", "Scene feature binding differs from outputs")
        if len(roots) != 1 or scene[roots[0]]["feature"] is not None:
            raise CadError("ASSEMBLY_INVALID", "Mechanical scene requires one nongeometric root")
        for node in scene:
            seen = set()
            current = node
            while current is not None:
                if current in seen or len(seen) > 64:
                    raise CadError("ASSEMBLY_INVALID", "Scene cycle or depth limit exceeded")
                seen.add(current)
                current = scene[current]["parent"]
            if node in outputs and any(n["parent"] == node for n in scene.values()):
                raise CadError("ASSEMBLY_INVALID", "Geometric instances must be scene leaves")
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
        from .features import execute_feature
        from .kernel import _require_shape, export_step, write_brep

        result["environment"] = environment()
        result["environment"]["evaluator_source_sha256"] = result["evaluator_source_sha256"]
        for module in ("features.py", "sketch.py", "assembly_io.py", "kernel.py"):
            result["environment"][module[:-3] + "_source_sha256"] = file_digest(
                Path(__file__).with_name(module)
            )
        mechanical = isinstance(goal, dict)
        write_json(directory / "plan.json", plan)
        write_json(directory / "goal.json", goal if mechanical else goal.to_dict())
        write_json(directory / "precision.json", precision.to_dict())
        shapes = {}
        feature_directory = directory / "features"
        feature_directory.mkdir()
        for feature in plan["features"]:
            parameters = feature["parameters"]
            op = feature["op"]
            diagnostic = {"feature_id": feature["id"], "op": op, "parameters": parameters}
            if op == "profile.sketch@1":
                from .sketch import build_sketch

                shape, diagnostic["sketch"] = build_sketch(feature["data"]["sketch"], precision)
            else:
                shape, diagnostic["kernel"] = execute_feature(feature, shapes, precision)
            _require_shape(shape, operation=feature["id"])
            shapes[feature["id"]] = shape
            path = feature_directory / f"{feature['id']}.brep"
            write_brep(shape, path)
            if path.stat().st_size > MAX_ARTIFACT_BYTES:
                raise CadError("RESOURCE_LIMIT", "Feature BREP exceeds 64 MiB")
            result["feature_outputs"][feature["id"]] = file_digest(path)
            operations.append(diagnostic)

        if mechanical:
            from .assembly_io import assembly_manifest, compound, export_assembly

            (directory / "nodes").mkdir()
            for node, feature in plan["outputs"].items():
                path = directory / "nodes" / f"{node}.brep"
                shutil.copyfile(feature_directory / f"{feature}.brep", path)
                result["node_outputs"][node] = file_digest(path)
            aggregate = compound(shapes[f] for f in plan["outputs"].values())
            write_brep(aggregate, directory / "model.brep")
            write_json(directory / "assembly.json", assembly_manifest(plan))
            exchange = export_assembly(
                plan, shapes, directory / "model.step", precision.working_tolerance_mm
            )
        else:
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
            if mechanical:
                report = directory / f"{action}.json"
                with (directory / f"{action}.log").open("wb") as log:
                    subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "deep3d_cad.mechanical_verifier",
                            "--directory",
                            str(directory),
                            "--stage",
                            "native" if action == "verify-brep" else "step",
                            "--report",
                            str(report),
                        ],
                        check=False,
                        stdout=log,
                        stderr=log,
                        timeout=timeout,
                    )
                result["stages"][action] = read_json(report, max_bytes=8 * 1024 * 1024)
            else:
                result["stages"][action] = run_worker(action, directory, timeout=timeout)
        passed = all(s["status"] == "pass" for s in result["stages"].values())
        if passed:
            validator = result["stages"]["verify-brep"]["validator_source_sha256"]
            if validator != result["stages"]["verify-step"]["validator_source_sha256"]:
                raise CadError("ENGINE_CHANGED", "Verifier build changed between checks")
            result["environment"]["verifier_source_sha256"] = validator
        else:
            result["error"] = {
                "code": "VERIFICATION_REJECTED",
                "message": "Independent BREP/STEP checks did not pass",
            }
        result["status"] = "pass" if passed else "fail"
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
