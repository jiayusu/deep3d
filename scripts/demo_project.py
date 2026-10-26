"""Exercise native persistence with a new Rust CLI process at every step."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    binary = args.binary or ROOT / "target/debug" / (
        "deep3d.exe" if sys.platform == "win32" else "deep3d"
    )
    binary = binary.resolve(strict=True)
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=False)

    def invoke(*command: str) -> dict | list:
        process = subprocess.run(
            [str(binary), *command], check=False, capture_output=True, text=True, timeout=180
        )
        if process.returncode != 0:
            raise RuntimeError(process.stdout + process.stderr)
        payload = json.loads(process.stdout)
        if payload["status"] != "pass":
            raise RuntimeError(payload)
        return payload["result"]

    project = output / "flange.deep3d"
    created = invoke("create", "--project", str(project), "--name", "Editable flange")
    original_id = created["revision"]["id"]
    first = invoke(
        "evaluate",
        "--project",
        str(project),
        "--python",
        str(args.python.resolve()),
        "--out",
        str(output / "original"),
    )
    changed = invoke(
        "edit",
        "--project",
        str(project),
        "--set",
        "thickness_mm=16",
        "--set",
        "bolt_circle_diameter_mm=80",
        "--message",
        "Increase thickness and bolt spacing",
    )
    second = invoke(
        "evaluate",
        "--project",
        str(project),
        "--python",
        str(args.python.resolve()),
        "--out",
        str(output / "edited"),
    )
    old = invoke("show", "--project", str(project), "--revision", original_id)
    reopened = invoke("show", "--project", str(project))
    history = invoke("history", "--project", str(project))
    assert len(history) == 2
    assert old["snapshot"]["project"] == created["project"]
    assert reopened["snapshot"]["project"] == changed["project"]
    assert set(created["project"]["features"]) == set(changed["project"]["features"])
    portable = output / "portable.deep3d"
    shutil.copyfile(project, portable)
    for label, evaluated in [("original", first), ("edited", second)]:
        target = output / f"{label}.step"
        invoke(
            "export",
            "--project",
            str(portable),
            "--materialization",
            evaluated["materialization"],
            "--out",
            str(target),
        )
        assert digest(target) == digest(output / label / "flange.step")
    measures = {}
    for label in ("original", "edited"):
        report = json.loads((output / label / "verify-step.json").read_text(encoding="utf-8"))
        checks = {item["id"]: item for item in report["checks"]}
        measures[label] = {
            "status": report["status"],
            "thickness_mm": checks["outer.z_max"]["observed"],
            "first_bolt_center_x_mm": checks["bolt_hole.0.center_x"]["observed"],
            "volume_mm3": report["measurements"]["volume_mm3"],
        }
    assert measures["original"]["thickness_mm"] == 12
    assert measures["edited"]["thickness_mm"] == 16
    result = {
        "schema_version": "deep3d.native-project-demo/1",
        "status": "pass",
        "scope": "local_flange_engineering_kernel",
        "project_id": created["project"]["project_id"],
        "project": "flange.deep3d",
        "project_sha256": digest(project),
        "history": history,
        "original_materialization": first["materialization"],
        "edited_materialization": second["materialization"],
        "head": reopened["snapshot"]["head"],
        "feature_count": len(created["project"]["features"]),
        "stable_feature_ids": True,
        "fresh_cli_process_per_step": True,
        "portable_export_exact_bytes": True,
        "measurements": measures,
        "industrial_qualification": "not_established",
    }
    (output / "demo.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps({"status": "pass", "project": str(project), "report": str(output / "demo.json")})
    )


if __name__ == "__main__":
    main()
