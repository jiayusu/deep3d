"""Archive completed native-kernel tests and portable-project evidence."""

import argparse
import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for part in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(part)
    return "sha256:" + hasher.hexdigest()


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def test_summary(path: Path, language: str) -> dict:
    text = path.read_text(encoding="utf-8-sig")
    if language == "rust":
        matches = re.findall(r"test result: ok\. (\d+) passed; 0 failed;", text)
        if not matches or "test result: FAILED" in text:
            raise ValueError(f"No passing Rust test result: {path}")
        count = sum(map(int, matches))
    else:
        match = re.search(r"(?m)^(\d+) passed in [\d.]+s", text)
        if not match:
            raise ValueError(f"No passing Python test result: {path}")
        count = int(match[1])
    return {"passed": count, "log_sha256": digest(path)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=ROOT / "artifacts/m1")
    parser.add_argument("--image", default="deep3d-cad:m1")
    parser.add_argument("--out", type=Path, default=ROOT / "qualification/m1/summary.json")
    args = parser.parse_args()
    reports = {}
    for platform in ("windows", "linux"):
        directory = args.artifacts / f"{platform}-project-demo"
        demo = read(directory / "demo.json")
        if demo["status"] != "pass" or not demo["portable_export_exact_bytes"]:
            raise ValueError("Project demo did not pass")
        if digest(directory / "flange.deep3d") != demo["project_sha256"]:
            raise ValueError("Project changed after the demo")
        reports[platform] = demo
    exported = args.artifacts / "windows-project-on-linux.step"
    if digest(exported) != digest(args.artifacts / "windows-project-demo/edited.step"):
        raise ValueError("Cross-platform native export differs")
    source_files = sorted(
        [
            *ROOT.glob("crates/**/*.rs"),
            *ROOT.glob("crates/**/Cargo.toml"),
            *ROOT.glob("workers/cad/src/deep3d_cad/*.py"),
            *ROOT.glob("tests/*.py"),
            ROOT / "Cargo.toml",
            ROOT / "Cargo.lock",
            ROOT / "pyproject.toml",
            ROOT / "uv.lock",
            ROOT / "requirements.lock",
            ROOT / "deploy/cad/Dockerfile",
            ROOT / "scripts/demo_project.py",
            ROOT / "scripts/cargo.ps1",
        ]
    )
    result = {
        "schema_version": "deep3d.kernel-smoke/1",
        "created_at": datetime.now(UTC).isoformat(),
        "status": "pass",
        "scope": "local_single_branch_engineering_kernel_and_analytic_flange",
        "milestones_complete": [],
        "industrial_qualification": "not_established",
        "windows": {
            "rust_tests": test_summary(args.artifacts / "rust-windows.log", "rust"),
            "python_tests": test_summary(args.artifacts / "python-windows.log", "python"),
            "native_project_demo": reports["windows"],
        },
        "linux": {
            "rust_core_tests": test_summary(args.artifacts / "rust-linux.log", "rust"),
            "python_tests": test_summary(args.artifacts / "python-linux.log", "python"),
            "native_project_demo": reports["linux"],
            "rust_binary_sha256": digest(args.artifacts / "linux-rust-target/debug/deep3d"),
            "runtime_image_id": subprocess.check_output(
                ["docker", "image", "inspect", args.image, "--format", "{{.Id}}"], text=True
            ).strip(),
            "runtime_policy": {
                "network": "none",
                "root_fs_read_only": True,
                "user": "10001:10001",
                "cpus": 2,
                "memory_bytes": 1073741824,
                "pids_limit": 128,
            },
        },
        "cross_platform_windows_package_export_on_linux": {
            "status": "pass",
            "step_sha256": digest(exported),
        },
        "source_files": {p.relative_to(ROOT).as_posix(): digest(p) for p in source_files},
        "remaining": [
            "PostgreSQL/cloud publication",
            "durable Agent runtime",
            "incremental execution/cache",
            "large-project incremental I/O",
            "full CAD feature set",
            "industrial qualification",
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "pass", "record": str(args.out)}))


if __name__ == "__main__":
    main()
