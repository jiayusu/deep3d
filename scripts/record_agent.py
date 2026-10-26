"""Archive local Agent evidence, keeping live model status separate from fixtures."""

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def tests(path: Path, kind: str) -> dict:
    content = path.read_text(encoding="utf-8-sig")
    if kind == "rust":
        counts = re.findall(r"test result: ok\. (\d+) passed; 0 failed;", content)
        if not counts or "test result: FAILED" in content:
            raise ValueError(f"Rust tests did not pass: {path.name}")
        count = sum(map(int, counts))
    else:
        match = re.search(r"(?m)^(\d+) passed in [\d.]+s", content)
        if not match:
            raise ValueError(f"Python tests did not pass: {path.name}")
        count = int(match[1])
    return {"passed": count, "log_sha256": digest(path)}


def demo(path: Path) -> dict:
    report = read(path / "demo.json")
    if (
        report["status"] != "pass"
        or report["live_llm"]
        or not report["completed_resume_idempotent"]
        or report["history_count"] != 2
    ):
        raise ValueError("Expected the explicitly scripted, successful CAD demonstration")
    result = report["result"]
    if digest(path / "run/delivery.step") != result["step_sha256"]:
        raise ValueError("STEP changed after the demonstration")
    run = read(path / "run/run.json")
    verified = path / "run" / run["attempts"][0]["evaluation_output"]
    evidence = {}
    for filename in ("verify-brep.json", "verify-step.json"):
        item = read(verified / filename)
        if (
            item["status"] != "pass"
            or not item["checks"]
            or not all(c["status"] == "pass" for c in item["checks"])
        ):
            raise ValueError("Geometry evidence did not pass")
        evidence[filename] = {
            "sha256": digest(verified / filename),
            "input_artifact_sha256": item["input_artifact_sha256"],
        }
    return {
        "live_llm": False,
        "model_requests": report["model_requests"],
        "geometry_jobs": report["geometry_jobs"],
        "history_count": report["history_count"],
        "completed_resume_idempotent": True,
        "revision_id": result["revision_id"],
        "materialization": result["materialization"],
        "step_sha256": result["step_sha256"],
        "parameters_mm": result["parameters_mm"],
        "geometry_evidence": evidence,
        "native_project_sha256": digest(path / "flange.deep3d"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=ROOT / "artifacts/agent-local")
    parser.add_argument("--out", type=Path, default=ROOT / "qualification/agent-local/summary.json")
    args = parser.parse_args()
    artifacts = args.artifacts
    live = read(artifacts / "live-attempt/demo.json")
    if live["status"] != "error" or live["code"] != "PROVIDER_AUTH":
        raise ValueError("Update the evidence recorder for the actual live-model result")
    live_run = read(artifacts / "live-attempt/run/run.json")
    if live_run["status"] != "waiting_provider" or live_run["attempts"]:
        raise ValueError("Unexpected live run state")
    sources = sorted(
        {
            *(ROOT / "crates").rglob("*.rs"),
            *(ROOT / "runtime/agent").rglob("*.py"),
            *(ROOT / "workers/cad/src").rglob("*.py"),
            *(ROOT / "tests").glob("*.py"),
            ROOT / "Cargo.lock",
            ROOT / "uv.lock",
            ROOT / "pyproject.toml",
            ROOT / "specs/agent-goal.schema.json",
            ROOT / "fixtures/agent/thicker-flange.json",
            ROOT / "scripts/demo_agent.py",
            ROOT / "scripts/record_agent.py",
        }
    )
    record = {
        "schema_version": "deep3d.agent-smoke/1",
        "created_at": datetime.now(UTC).isoformat(),
        "local_tool_loop": "pass",
        "live_llm": {
            "status": "blocked",
            "code": live["code"],
            "model_requests": live_run["turns"],
            "requested_model": live_run["model"],
            "run_state": live_run["status"],
            "reason": "Configured HTTPS service rejected authentication/access; no model tools ran",
        },
        "industrial_qualification": "not_established",
        "milestones_complete": [],
        "windows": {
            "rust_workspace_tests": tests(artifacts / "rust-windows.log", "rust"),
            "python_full_suite": tests(artifacts / "python-windows.log", "python"),
            "scripted_model_real_cad": demo(artifacts / "offline-demo-final"),
        },
        "linux": {
            "rust_domain_storage_tests": tests(artifacts / "rust-linux.log", "rust"),
            "python_agent_suite": tests(artifacts / "agent-linux.log", "python"),
            "scripted_model_real_cad": demo(artifacts / "linux-offline-demo"),
        },
        "source_digests": {path.relative_to(ROOT).as_posix(): digest(path) for path in sources},
        "limitations": [
            "local synchronous process with durable checkpoints",
            "typed independent goal required; five flange length parameters only",
            "no PostgreSQL/Temporal/cloud publication/industrial qualification",
            "live model success and provider billing not established",
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"local_tool_loop": "pass", "live_llm": "blocked", "record": str(args.out)}))


if __name__ == "__main__":
    main()
