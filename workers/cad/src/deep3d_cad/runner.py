"""Process-isolated CAD smoke/qualification runs with saved input snapshots."""

import hashlib
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .contracts import CadError, FlangeSpec, PrecisionProfile
from .jsonio import MAX_REPORT_BYTES, read_json, write_json

DEFAULT_TIMEOUT_SECONDS = 120


def file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


def run_worker(action: str, directory: Path, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict:
    if action not in {"build", "verify-brep", "verify-step"}:
        raise CadError("CAPABILITY_UNAVAILABLE", "Unknown worker action")
    report_path = directory / f"{action}.json"
    args = [
        sys.executable,
        "-m",
        "deep3d_cad.worker",
        action,
        "--spec",
        str(directory / ("candidate.json" if action == "build" else "goal.json")),
        "--precision",
        str(directory / "precision.json"),
        "--report",
        str(report_path),
    ]
    if action == "build":
        args += ["--directory", str(directory)]
    else:
        suffix = "brep" if action == "verify-brep" else "step"
        args += ["--shape", str(directory / f"flange.{suffix}")]
    log_path = directory / f"{action}.log"
    try:
        with log_path.open("xb") as log:
            process = subprocess.run(
                args,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
    except subprocess.TimeoutExpired as exc:
        raise CadError(
            "WORKER_TIMEOUT",
            "CAD worker exceeded its wall-clock limit",
            {"action": action, "timeout_seconds": timeout},
        ) from exc
    if not report_path.is_file():
        raise CadError(
            "WORKER_INTERRUPTED",
            "Worker exited without a result",
            {"action": action, "returncode": process.returncode},
        )
    report = read_json(report_path, max_bytes=MAX_REPORT_BYTES)
    if process.returncode not in {0, 2} or report.get("status") == "error":
        raise CadError(
            "WORKER_FAILED",
            "CAD worker failed",
            {"action": action, "returncode": process.returncode, "result": report},
        )
    if (process.returncode == 0) != (report.get("status") == "pass"):
        raise CadError("WORKER_PROTOCOL_ERROR", "Exit code and report disagree")
    return report


def run_flange(
    candidate: FlangeSpec,
    output: Path,
    *,
    goal: FlangeSpec | None = None,
    precision: PrecisionProfile | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    goal = goal or candidate
    precision = precision or PrecisionProfile()
    output = output.resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise CadError(
            "OUTPUT_EXISTS",
            "Use a new output directory; existing artifacts are preserved",
            {"path": str(output)},
        ) from exc
    report = {
        "schema_version": "deep3d.cad-run/1",
        "run_id": str(uuid4()),
        "created_at": datetime.now(UTC).isoformat(),
        "status": "error",
        "scope": "analytic_flange_smoke_not_industrial_qualification",
        "unit": "mm",
        "coordinate_system": "right_handed_z_up",
        "isolation": "fresh_process_per_action_not_an_untrusted_code_sandbox",
        "stages": {},
    }
    try:
        write_json(output / "candidate.json", candidate.to_dict())
        write_json(output / "goal.json", goal.to_dict())
        write_json(output / "precision.json", precision.to_dict())
        for action in ("build", "verify-brep", "verify-step"):
            report["stages"][action] = run_worker(action, output, timeout=timeout)
        report["status"] = (
            "pass" if all(r["status"] == "pass" for r in report["stages"].values()) else "fail"
        )
    except CadError as exc:
        report["error"] = exc.to_dict()
    report["artifacts"] = [
        {"path": p.name, "sha256": file_digest(p), "size_bytes": p.stat().st_size}
        for p in sorted(output.iterdir())
        if p.is_file() and not p.name.endswith(".tmp")
    ]
    write_json(output / "report.json", report)
    return report


def qualify(output: Path, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict:
    output = output.resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise CadError("OUTPUT_EXISTS", "Qualification output already exists") from exc
    goal = FlangeSpec()
    cases = [
        ("nominal", goal, "pass"),
        ("wrong_hole_diameter", replace(goal, bolt_hole_diameter_mm=9.0), "fail"),
        ("wrong_hole_positions", replace(goal, bolt_circle_diameter_mm=80.0), "fail"),
        ("missing_hole", replace(goal, hole_count=3), "fail"),
        ("wrong_thickness", replace(goal, thickness_mm=14.0), "fail"),
        ("edited_goal", replace(goal, thickness_mm=16.0, bolt_circle_diameter_mm=80.0), "pass"),
    ]
    summary = {
        "schema_version": "deep3d.qualification/1",
        "status": "pass",
        "scope": "M0_flange_smoke_only",
        "industrial_qualification": "not_performed",
        "target_cad_interoperability": "not_performed",
        "cases": [],
    }
    for name, candidate, expected_status in cases:
        current_goal = candidate if name == "edited_goal" else goal
        result = run_flange(candidate, output / name, goal=current_goal, timeout=timeout)
        stages = result.get("stages", {})
        matched = result["status"] == expected_status and (
            expected_status == "pass"
            or all(
                stages.get(stage, {}).get("status") == "fail"
                for stage in ("verify-brep", "verify-step")
            )
        )
        summary["cases"].append(
            {
                "case": name,
                "expected": expected_status,
                "observed": result["status"],
                "status": "pass" if matched else "fail",
                "report": f"{name}/report.json",
            }
        )
        if not matched:
            summary["status"] = "fail"
    write_json(output / "qualification.json", summary)
    return summary
