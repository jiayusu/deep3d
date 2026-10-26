"""Record completed smoke evidence; never turns missing tests into a pass."""

import argparse
import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def tests(path: Path) -> dict:
    text = path.read_text(encoding="utf-8-sig")
    result = re.search(r"(?m)^(\d+) passed in ([\d.]+)s", text)
    if result is None or re.search(r"(?m)^.*\d+ (failed|error|errors)(?:,| in )", text):
        raise ValueError(f"No clean passing pytest summary in {path}")
    return {"passed": int(result[1]), "seconds": float(result[2]), "log_sha256": digest(path)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-log", type=Path, required=True)
    parser.add_argument("--linux-log", type=Path, required=True)
    parser.add_argument("--linux-results", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    smoke = json.loads((args.linux_results / "qualification.json").read_text(encoding="utf-8"))
    if smoke["status"] != "pass" or not all(c["status"] == "pass" for c in smoke["cases"]):
        raise ValueError("Linux smoke has unfulfilled expectations")
    step_report = json.loads(
        (args.linux_results / "nominal/verify-step.json").read_text(encoding="utf-8")
    )
    if step_report["status"] != "pass":
        raise ValueError("Nominal STEP was not accepted")
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", args.image, "--format", "{{.Id}}"], text=True
    ).strip()
    os_packages = subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            image_id,
            "dpkg-query",
            "-W",
            "-f",
            "${Package}=${Version}\n",
        ],
        text=True,
    ).splitlines()
    source_files = sorted(
        [
            *ROOT.glob("workers/cad/src/deep3d_cad/*.py"),
            *ROOT.glob("tests/*.py"),
            *ROOT.glob("specs/*.json"),
            ROOT / "fixtures/flange/standard.json",
            ROOT / "pyproject.toml",
            ROOT / "uv.lock",
            ROOT / "requirements.lock",
            ROOT / "deploy/cad/Dockerfile",
        ]
    )
    dimension_errors = [
        c["details"]["absolute_error_mm"]
        for c in step_report["checks"]
        if isinstance(c.get("details"), dict) and "absolute_error_mm" in c["details"]
    ]
    result = {
        "schema_version": "deep3d.smoke-record/1",
        "created_at": datetime.now(UTC).isoformat(),
        "status": "pass",
        "qualified_scope": "analytic_flange_smoke_only",
        "milestones_complete": [],
        "industrial_qualification": "not_established",
        "independent_target_cad": "not_tested",
        "windows_tests": tests(args.native_log),
        "linux_tests": tests(args.linux_log),
        "linux_smoke": smoke,
        "linux_environment": step_report["environment"],
        "linux_container": {
            "image_id": image_id,
            "base_image": "python:3.12.12-slim-bookworm",
            "base_digest": (
                "sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c"
            ),
            "runtime_constraints": {
                "network": "none",
                "cpus": 2,
                "memory_bytes": 1073741824,
                "pids_limit": 128,
                "root_fs_read_only": True,
                "user": "10001:10001",
                "capabilities": [],
                "no_new_privileges": True,
            },
            "os_packages": sorted(os_packages),
            "apt_snapshot_pinned": False,
        },
        "nominal_step": {
            "volume_mm3": step_report["measurements"]["volume_mm3"],
            "maximum_analytic_dimension_error_mm": max(dimension_errors),
            "measurement_guard_band_mm": 0.0001,
            "independently_calibrated_metrology_bound": False,
            "sha256": digest(args.linux_results / "nominal/flange.step"),
        },
        "source_files": {p.relative_to(ROOT).as_posix(): digest(p) for p in source_files},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": result["status"], "record": str(args.out)}))


if __name__ == "__main__":
    main()
