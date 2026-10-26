"""Run the first bounded CAD Agent against a new, real native project.

The default transport is a live Anthropic-compatible model. Explicit --scripted
replays tool calls for offline reproducibility; it never claims a live LLM result.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from deep3d_agent.provider import AnthropicProvider, ProviderError
from deep3d_agent.runtime import AgentError, resume_agent, run_agent
from deep3d_cad.jsonio import read_json, write_json

ROOT = Path(__file__).resolve().parents[1]


class ScriptedDemo:
    model = "scripted-demo-not-a-live-llm"

    def __init__(self, goal: dict):
        self.turn = 0
        self.goal = goal

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> dict:
        self.turn += 1
        actions = [
            ("inspect_project", {}),
            (
                "evaluate_parameters",
                {
                    "changes": [
                        {"name": name, "value_mm": value}
                        for name, value in self.goal["set_parameters_mm"].items()
                    ],
                    "rationale": "Reproduce the independent goal using the real CAD kernel",
                },
            ),
            ("accept_candidate", {"attempt": 1}),
        ]
        name, arguments = actions[self.turn - 1]
        return {
            "content": [
                {"type": "tool_use", "id": f"demo-{self.turn}", "name": name, "input": arguments}
            ],
            "stop_reason": "tool_use",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--model", help="Required for a live model; credentials come from environment"
    )
    parser.add_argument("--scripted", action="store_true", help="Offline model fixture, real OCCT")
    parser.add_argument("--binary", type=Path)
    args = parser.parse_args()
    if not args.scripted and not args.model:
        parser.error("A live model requires --model; use --scripted only for offline reproduction")
    binary = (
        args.binary
        or ROOT / "target/debug" / ("deep3d.exe" if sys.platform == "win32" else "deep3d")
    ).resolve(strict=True)
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=False)
    project = output / "flange.deep3d"
    goal = read_json(ROOT / "fixtures/agent/thicker-flange.json")

    def invoke(*command: str) -> dict | list:
        process = subprocess.run(
            [str(binary), *command], check=False, capture_output=True, encoding="utf-8", timeout=180
        )
        payload = json.loads(process.stdout)
        if process.returncode or payload["status"] != "pass":
            raise RuntimeError(payload)
        return payload["result"]

    initial = invoke("create", "--project", str(project), "--name", "Agent flange")
    try:
        provider = ScriptedDemo(goal) if args.scripted else AnthropicProvider(model=args.model)
        result = run_agent(
            project=project,
            request="把法兰厚度改为 16 mm，孔分布圆直径改为 80 mm，其余保持。",
            goal=goal,
            run_dir=output / "run",
            binary=binary,
            python=Path(sys.executable),
            provider=provider,
        )
        resumed = resume_agent(
            run_dir=output / "run", binary=binary, python=Path(sys.executable), provider=provider
        )
        assert resumed == result
        history = invoke("history", "--project", str(project))
        assert len(history) == 2 and history[-1]["id"] == result["revision_id"]
        old = invoke("show", "--project", str(project), "--revision", initial["revision"]["id"])
        assert old["snapshot"]["project"] == initial["project"]
        checkpoint = read_json(output / "run/run.json")
        report = {
            "schema_version": "deep3d.agent-demo/1",
            "status": "pass",
            "live_llm": not args.scripted,
            "model": provider.model,
            "project": str(project),
            "model_requests": checkpoint["turns"],
            "geometry_jobs": checkpoint["geometry_jobs"],
            "history_count": len(history),
            "completed_resume_idempotent": True,
            "result": result,
        }
        write_json(output / "demo.json", report)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except (AgentError, ProviderError) as exc:
        report = {
            "schema_version": "deep3d.agent-demo/1",
            "status": "error",
            "live_llm": not args.scripted,
            "code": exc.code,
            "message": str(exc),
            "project": str(project),
        }
        write_json(output / "demo.json", report)
        print(json.dumps(report, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
