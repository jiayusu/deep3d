"""Script the model transport while exercising real Rust/OCCT and recovery boundaries.

These providers are orchestration fixtures, not evidence of live LLM integration.
Geometry, native revisions, materializations and exported STEP files are never mocked.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from deep3d_agent import runtime as runtime_module
from deep3d_agent.runtime import AgentError, resume_agent, run_agent, validate_goal
from deep3d_cad.contracts import FlangeSpec, PrecisionProfile

ROOT = Path(__file__).resolve().parents[1]
GOAL = {
    "schema_version": "deep3d.agent-goal/1",
    "set_parameters_mm": {"thickness_mm": 16, "bolt_circle_diameter_mm": 80},
    "preserve_other_parameters": True,
}
CHANGES = [
    {"name": "thickness_mm", "value_mm": 16},
    {"name": "bolt_circle_diameter_mm", "value_mm": 80},
]


def tool(name: str, arguments: dict | None = None) -> dict:
    return {
        "content": [
            {"type": "tool_use", "id": str(uuid4()), "name": name, "input": arguments or {}}
        ],
        "stop_reason": "tool_use",
    }


def evaluate(changes: list[dict] | None = None) -> dict:
    return tool(
        "evaluate_parameters",
        {"changes": copy.deepcopy(CHANGES if changes is None else changes), "rationale": "Goal"},
    )


def end_turn() -> dict:
    return {"content": [{"type": "text", "text": "Unable to continue"}], "stop_reason": "end_turn"}


class ScriptedProvider:
    """Queue tool calls, failure injection or callbacks; retain the actual tool results."""

    model = "scripted-integration-fixture"

    def __init__(self, *actions: object):
        self.actions = list(actions)
        self.requests: list[dict] = []

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> dict:
        self.requests.append(
            copy.deepcopy({"system": system, "messages": messages, "tools": tools})
        )
        assert self.actions, "Runtime made an unexpected model request"
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        if callable(action):
            action = action()
        return copy.deepcopy(action)

    def result_payloads(self) -> list[dict]:
        payloads = []
        for request in self.requests:
            for message in request["messages"]:
                if not isinstance(message["content"], list):
                    continue
                for block in message["content"]:
                    if block["type"] == "tool_result":
                        payloads.append(json.loads(block["content"]))
        return payloads


@pytest.fixture
def binary() -> Path:
    executable = ROOT / "target/debug" / ("deep3d.exe" if sys.platform == "win32" else "deep3d")
    if not executable.is_file():
        pytest.skip("Build the real Rust CLI before running Agent integration tests")
    return executable


def cli(binary: Path, *arguments: object) -> dict | list:
    process = subprocess.run(
        [str(binary), *(str(argument) for argument in arguments)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        check=False,
    )
    value = json.loads(process.stdout)
    assert process.returncode == 0, value
    assert value["status"] == "pass", value
    return value["result"]


def new_project(binary: Path, directory: Path) -> Path:
    project = directory / "flange.deep3d"
    cli(binary, "create", "--project", project)
    return project


def start(
    binary: Path, project: Path, run_dir: Path, provider: ScriptedProvider, **options
) -> dict:
    return run_agent(
        project=project,
        request="Change thickness to 16 mm and bolt circle to 80 mm; preserve everything else.",
        goal=copy.deepcopy(GOAL),
        run_dir=run_dir,
        binary=binary,
        python=Path(sys.executable),
        provider=provider,
        **options,
    )


def state(run_dir: Path) -> dict:
    return json.loads((run_dir / "run.json").read_text(encoding="utf-8"))


def test_scripted_model_repairs_goal_mismatch_then_delivers_real_step(binary, tmp_path):
    project = new_project(binary, tmp_path)
    original = cli(binary, "show", "--project", project)["snapshot"]
    original_bytes = project.read_bytes()
    run_dir = tmp_path / "run"
    wrong = copy.deepcopy(CHANGES)
    wrong[0]["value_mm"] = 18

    def accept_after_checking_isolation():
        checkpoint = state(run_dir)
        assert checkpoint["attempts"][0]["status"] == "verified"
        assert project.read_bytes() == original_bytes
        return tool("accept_candidate", {"attempt": 1})

    provider = ScriptedProvider(
        tool("inspect_project"), evaluate(wrong), evaluate(), accept_after_checking_isolation
    )
    result = start(binary, project, run_dir, provider)
    assert result["status"] == "completed"
    assert any(item.get("code") == "GOAL_MISMATCH" for item in provider.result_payloads())
    assert not provider.actions
    assert state(run_dir)["geometry_jobs"] == 1
    current = cli(binary, "show", "--project", project)
    assert current["snapshot"]["revision"]["id"] == result["revision_id"]
    expected_project = copy.deepcopy(original["project"])
    for parameter in expected_project["parameters"].values():
        if parameter["name"] in GOAL["set_parameters_mm"]:
            parameter["value"]["value"] = GOAL["set_parameters_mm"][parameter["name"]]
    assert current["snapshot"]["project"] == expected_project
    assert len(cli(binary, "history", "--project", project)) == 2
    assert current["materializations"][0][0] == result["materialization"]
    delivery = Path(result["delivery"])
    assert delivery.is_file()
    assert result["step_sha256"] == "sha256:" + hashlib.sha256(delivery.read_bytes()).hexdigest()

    # Independently re-open the delivery, rather than trusting a saved pass flag.
    spec_file, precision_file = tmp_path / "goal.json", tmp_path / "precision.json"
    spec_file.write_text(
        json.dumps(FlangeSpec(thickness_mm=16, bolt_circle_diameter_mm=80).to_dict()),
        encoding="utf-8",
    )
    precision_file.write_text(json.dumps(PrecisionProfile().to_dict()), encoding="utf-8")
    report_file = tmp_path / "delivery-verification.json"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "deep3d_cad.worker",
            "verify-step",
            "--shape",
            str(delivery),
            "--spec",
            str(spec_file),
            "--precision",
            str(precision_file),
            "--report",
            str(report_file),
        ],
        check=False,
        capture_output=True,
        timeout=120,
    )
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert process.returncode == 0, report
    assert report["status"] == "pass"
    checks = {item["id"]: item for item in report["checks"]}
    assert checks["outer.z_max"]["observed"] == 16
    assert checks["bolt_hole.0.center_x"]["observed"] == 40
    assert all(check["status"] == "pass" for check in report["checks"])


@pytest.mark.parametrize(
    ("proposal", "code"),
    [
        (evaluate([{"name": "thickness_mm", "value_mm": 17}]), "GOAL_MISMATCH"),
        (tool("execute_python", {"script": "print('unsupported')"}), "TOOL_FORBIDDEN"),
    ],
)
def test_rejected_model_action_cannot_change_source(binary, tmp_path, proposal, code):
    project = new_project(binary, tmp_path)
    before = project.read_bytes()
    provider = ScriptedProvider(tool("inspect_project"), proposal, end_turn())
    run_dir = tmp_path / "run"
    with pytest.raises(AgentError) as failure:
        start(binary, project, run_dir, provider)
    assert failure.value.code == "AGENT_INCOMPLETE"
    assert any(item.get("code") == code for item in provider.result_payloads())
    assert project.read_bytes() == before
    assert len(cli(binary, "history", "--project", project)) == 1
    assert state(run_dir)["attempts"] == []
    assert not (run_dir / "delivery.step").exists()


def test_real_geometry_rejection_cannot_be_accepted_or_change_source(binary, tmp_path):
    project = new_project(binary, tmp_path)
    shown = cli(binary, "show", "--project", project)["snapshot"]
    feature = next(
        copy.deepcopy(item)
        for item in shown["project"]["features"].values()
        if item["name"] == "bolt_hole_0_profile"
    )
    feature["operation"]["radius"] = {"kind": "literal", "value": {"kind": "length_mm", "value": 5}}
    transaction_file = tmp_path / "wrong-feature.json"
    transaction_file.write_text(
        json.dumps(
            {
                "id": str(uuid4()),
                "schema_version": "deep3d.transaction/1",
                "base_revision": shown["head"]["revision_id"],
                "expected_generation": shown["head"]["generation"],
                "message": "Model a wrong hole while leaving the independent design goal unchanged",
                "commands": [{"op": "feature.replace", "feature": feature}],
            }
        ),
        encoding="utf-8",
    )
    cli(binary, "apply", "--project", project, "--transaction", transaction_file)
    before = project.read_bytes()
    run_dir = tmp_path / "run"
    provider = ScriptedProvider(
        tool("inspect_project"), evaluate(), tool("accept_candidate", {"attempt": 1}), end_turn()
    )
    with pytest.raises(AgentError) as failure:
        start(binary, project, run_dir, provider)
    assert failure.value.code == "AGENT_INCOMPLETE"
    codes = {item.get("code") for item in provider.result_payloads()}
    assert {"EVALUATION_FAILED", "CANDIDATE_UNVERIFIED"} <= codes
    assert project.read_bytes() == before
    checkpoint = state(run_dir)
    assert checkpoint["attempts"][0]["status"] == "failed"
    candidate = run_dir / checkpoint["attempts"][0]["candidate"]
    assert cli(binary, "show", "--project", candidate)["materializations"] == []
    assert not (run_dir / "delivery.step").exists()


def test_provider_failure_resumes_verified_candidate_and_completion_is_idempotent(binary, tmp_path):
    project = new_project(binary, tmp_path)
    before = project.read_bytes()
    run_dir = tmp_path / "run"
    provider = ScriptedProvider(
        tool("inspect_project"), evaluate(), RuntimeError("PRIVATE credential or server body")
    )
    with pytest.raises(AgentError) as failure:
        start(binary, project, run_dir, provider)
    assert failure.value.code == "PROVIDER_UNAVAILABLE"
    assert state(run_dir)["status"] == "waiting_provider"
    assert state(run_dir)["attempts"][0]["status"] == "verified"
    assert "PRIVATE" not in (run_dir / "run.json").read_text(encoding="utf-8")
    assert project.read_bytes() == before
    resumed_provider = ScriptedProvider(tool("accept_candidate", {"attempt": 1}))
    result = resume_agent(
        run_dir=run_dir, binary=binary, python=Path(sys.executable), provider=resumed_provider
    )
    assert result["status"] == "completed"
    assert state(run_dir)["geometry_jobs"] == 1
    accepted = project.read_bytes()
    no_more_calls = ScriptedProvider()
    assert (
        resume_agent(
            run_dir=run_dir, binary=binary, python=Path(sys.executable), provider=no_more_calls
        )
        == result
    )
    assert not no_more_calls.requests
    assert project.read_bytes() == accepted
    assert len(cli(binary, "history", "--project", project)) == 2


def test_concurrent_user_edit_rejects_candidate_without_losing_new_head(binary, tmp_path):
    project = new_project(binary, tmp_path)
    run_dir = tmp_path / "run"
    concurrent = {}

    def edit_before_accept():
        assert state(run_dir)["attempts"][0]["status"] == "verified"
        concurrent["snapshot"] = cli(
            binary, "edit", "--project", project, "--set", "thickness_mm=18"
        )
        concurrent["bytes"] = project.read_bytes()
        return tool("accept_candidate", {"attempt": 1})

    provider = ScriptedProvider(tool("inspect_project"), evaluate(), edit_before_accept)
    with pytest.raises(AgentError) as failure:
        start(binary, project, run_dir, provider)
    assert failure.value.code == "REVISION_CONFLICT"
    assert project.read_bytes() == concurrent["bytes"]
    current = cli(binary, "show", "--project", project)
    assert current["snapshot"] == concurrent["snapshot"]
    assert len(cli(binary, "history", "--project", project)) == 2
    assert current["materializations"] == []
    assert state(run_dir)["status"] == "failed"
    assert state(run_dir)["attempts"][0]["status"] == "verified"
    assert not (run_dir / "delivery.step").exists()


@pytest.mark.parametrize(
    ("event", "crash_before_save", "saved_status", "source_revision_count"),
    [
        ("candidate_verified", False, "running", 1),
        ("candidate_adopted", True, "adopting", 2),
    ],
)
def test_checkpoint_recovery_reuses_geometry_and_committed_adoption(
    binary, tmp_path, monkeypatch, event, crash_before_save, saved_status, source_revision_count
):
    class SimulatedProcessDeath(BaseException):
        pass

    project = new_project(binary, tmp_path)
    run_dir = tmp_path / "run"
    real_save = runtime_module._Runtime.save

    def crash_on_event(self, name, **details):
        if name == event and crash_before_save:
            raise SimulatedProcessDeath
        real_save(self, name, **details)
        if name == event:
            raise SimulatedProcessDeath

    provider = ScriptedProvider(
        tool("inspect_project"), evaluate(), tool("accept_candidate", {"attempt": 1})
    )
    with monkeypatch.context() as isolated_patch:
        isolated_patch.setattr(runtime_module._Runtime, "save", crash_on_event)
        with pytest.raises(SimulatedProcessDeath):
            start(binary, project, run_dir, provider)
    checkpoint = state(run_dir)
    assert checkpoint["status"] == saved_status
    assert checkpoint["geometry_jobs"] == 1
    assert checkpoint["attempts"][0]["status"] == "verified"
    assert len(cli(binary, "history", "--project", project)) == source_revision_count
    # Replaying the pending evaluation consumes its existing verified materialization;
    # replaying adoption finds its committed transaction instead of appending a revision.
    resumed_provider = (
        ScriptedProvider(tool("accept_candidate", {"attempt": 1}))
        if event == "candidate_verified"
        else ScriptedProvider()
    )
    result = resume_agent(
        run_dir=run_dir, binary=binary, python=Path(sys.executable), provider=resumed_provider
    )
    assert result["status"] == "completed"
    assert result["revision_id"] == checkpoint["attempts"][0]["revision_id"]
    assert state(run_dir)["geometry_jobs"] == 1
    assert len(cli(binary, "history", "--project", project)) == 2
    assert len(list(run_dir.glob("evaluation-*"))) == 1
    if event == "candidate_adopted":
        assert not resumed_provider.requests


@pytest.mark.parametrize("code", ["KERNEL_TIMEOUT", "KERNEL_PROTOCOL", "COMMIT_DURABILITY_UNKNOWN"])
def test_lost_native_adoption_reply_is_resumed_without_more_model_work(
    binary, tmp_path, monkeypatch, code
):
    project = new_project(binary, tmp_path)
    run_dir = tmp_path / "run"
    real_invoke = runtime_module._Runtime.invoke
    injected = False

    def lose_adoption_reply(self, *arguments):
        nonlocal injected
        result = real_invoke(self, *arguments)
        if arguments[0] == "adopt" and not injected:
            injected = True
            raise AgentError(code, "Simulated lost reply after native commit")
        return result

    provider = ScriptedProvider(
        tool("inspect_project"), evaluate(), tool("accept_candidate", {"attempt": 1})
    )
    with monkeypatch.context() as isolated_patch:
        isolated_patch.setattr(runtime_module._Runtime, "invoke", lose_adoption_reply)
        with pytest.raises(AgentError) as failure:
            start(binary, project, run_dir, provider)
    assert failure.value.code == code
    assert injected
    assert len(provider.requests) == 3
    assert not provider.actions
    checkpoint = state(run_dir)
    assert checkpoint["status"] == "adopting"
    assert checkpoint["error"]["code"] == code
    assert checkpoint["geometry_jobs"] == 1
    assert len(cli(binary, "history", "--project", project)) == 2
    committed = project.read_bytes()
    resumed_provider = ScriptedProvider()
    result = resume_agent(
        run_dir=run_dir, binary=binary, python=Path(sys.executable), provider=resumed_provider
    )
    assert result["status"] == "completed"
    assert result["revision_id"] == checkpoint["attempts"][0]["revision_id"]
    assert not resumed_provider.requests
    assert state(run_dir)["geometry_jobs"] == 1
    assert len(cli(binary, "history", "--project", project)) == 2
    assert project.read_bytes() == committed


@pytest.mark.parametrize("damage", ["missing", "corrupt", "interrupted_repair"])
def test_completed_resume_recovers_delivery_without_model_or_revision_changes(
    binary, tmp_path, monkeypatch, damage
):
    project = new_project(binary, tmp_path)
    run_dir = tmp_path / "run"
    provider = ScriptedProvider(
        tool("inspect_project"), evaluate(), tool("accept_candidate", {"attempt": 1})
    )
    original_result = start(binary, project, run_dir, provider)
    delivery = Path(original_result["delivery"])
    verified_bytes = delivery.read_bytes()
    committed = project.read_bytes()
    if damage == "missing":
        delivery.unlink()
    else:
        delivery.write_bytes(b"damaged STEP, not verified geometry")
    if damage == "interrupted_repair":

        class SimulatedProcessDeath(BaseException):
            pass

        real_save = runtime_module._Runtime.save

        def interrupt_after_recovery_checkpoint(self, event, **details):
            real_save(self, event, **details)
            if event == "delivery_recovery_requested":
                raise SimulatedProcessDeath

        with monkeypatch.context() as isolated_patch:
            isolated_patch.setattr(
                runtime_module._Runtime, "save", interrupt_after_recovery_checkpoint
            )
            with pytest.raises(SimulatedProcessDeath):
                resume_agent(
                    run_dir=run_dir,
                    binary=binary,
                    python=Path(sys.executable),
                    provider=ScriptedProvider(),
                )
        assert state(run_dir)["status"] == "delivering"
        assert delivery.read_bytes() == b"damaged STEP, not verified geometry"
    resumed_provider = ScriptedProvider()
    result = resume_agent(
        run_dir=run_dir, binary=binary, python=Path(sys.executable), provider=resumed_provider
    )
    assert result == original_result
    assert not resumed_provider.requests
    assert delivery.read_bytes() == verified_bytes
    assert project.read_bytes() == committed
    assert len(cli(binary, "history", "--project", project)) == 2
    assert state(run_dir)["geometry_jobs"] == 1
    assert state(run_dir)["status"] == "completed"
    if damage != "missing":
        quarantined = list(run_dir.glob("damaged-delivery-*.step"))
        assert len(quarantined) == 1
        assert quarantined[0].read_bytes() == b"damaged STEP, not verified geometry"


@pytest.mark.parametrize(
    "edit",
    [
        {"preserve_other_parameters": False},
        {"set_parameters_mm": {"thickness_mm": True}},
        {"set_parameters_mm": {"thickness_mm": float("nan")}},
        {"set_parameters_mm": {"tolerance_mm": 1}},
        {"set_parameters_mm": {}},
        {"schema_version": "deep3d.agent-goal/999"},
        {"extra": "model cannot add authority"},
    ],
)
def test_goal_contract_rejects_changes_to_scope_and_nonfinite_dimensions(edit):
    with pytest.raises(AgentError) as failure:
        validate_goal({**copy.deepcopy(GOAL), **edit})
    assert failure.value.code == "GOAL_INVALID"
