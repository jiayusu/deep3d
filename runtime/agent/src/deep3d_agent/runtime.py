"""A local Agent loop; the model proposes, deterministic services decide success.

Run checkpoints are trusted local data, not a multi-tenant authorization database.
Model calls and interrupted geometry jobs can repeat; adoption is idempotent in Rust.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from deep3d_cad.contracts import CadError
from deep3d_cad.jsonio import read_json, write_json

PARAMETERS = (
    "outer_diameter_mm",
    "thickness_mm",
    "center_hole_diameter_mm",
    "bolt_hole_diameter_mm",
    "bolt_circle_diameter_mm",
)
SYSTEM = """You are a precision CAD operator. Execute the user's request only within the
independent goal contract. First inspect_project; then evaluate_parameters using all and
only the exact goal changes in mm; then accept_candidate only after verified=true.
Do not claim success from text or a build alone. Read tool errors and correct your tool
arguments where possible. You cannot change the goal, tolerance, geometry operations,
files, budget or verification policy. Unmentioned engineering data must be preserved.
No other tools are available. Finish concisely; the runtime delivers the verified STEP.
"""


def _schema(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


TOOLS = [
    {
        "name": "inspect_project",
        "description": "Read the frozen base and authorized goal.",
        "input_schema": _schema({}, []),
    },
    {
        "name": "evaluate_parameters",
        "description": "Build a separate candidate and independently verify B-Rep and STEP.",
        "input_schema": _schema(
            {
                "changes": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 5,
                    "items": _schema(
                        {
                            "name": {"type": "string", "enum": list(PARAMETERS)},
                            "value_mm": {"type": "number"},
                        },
                        ["name", "value_mm"],
                    ),
                },
                "rationale": {"type": "string", "maxLength": 512},
            },
            ["changes", "rationale"],
        ),
    },
    {
        "name": "accept_candidate",
        "description": "Accept one verified attempt if the source version still matches.",
        "input_schema": _schema({"attempt": {"type": "integer", "minimum": 1}}, ["attempt"]),
    },
]


class AgentError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AgentError("GOAL_INVALID", "Parameter values must be finite numbers in mm")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise AgentError("GOAL_INVALID", "Parameter value exceeds finite f64") from exc
    if not math.isfinite(result):
        raise AgentError("GOAL_INVALID", "Parameter values must be finite numbers in mm")
    return result


def validate_goal(goal: dict) -> dict:
    if (
        not isinstance(goal, dict)
        or set(goal) != {"schema_version", "set_parameters_mm", "preserve_other_parameters"}
        or goal["schema_version"] != "deep3d.agent-goal/1"
        or goal["preserve_other_parameters"] is not True
    ):
        raise AgentError("GOAL_INVALID", "Expected the independent deep3d.agent-goal/1 contract")
    changes = goal["set_parameters_mm"]
    if not isinstance(changes, dict) or not changes or not set(changes).issubset(PARAMETERS):
        raise AgentError("GOAL_INVALID", "Goal must name one to five supported length parameters")
    return {**goal, "set_parameters_mm": {name: _number(v) for name, v in changes.items()}}


def _digest(value: dict) -> str:
    data = json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _save(path: Path, value: dict) -> None:
    write_json(path, value)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@contextmanager
def _run_lock(directory: Path):
    # Never unlink: a stable lock inode prevents two resumed writers crossing.
    with (directory / "run.lock").open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AgentError("RUN_BUSY", "Another process owns this run") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class _Runtime:
    def __init__(self, directory: Path, binary: Path, python: Path, provider: object):
        self.directory = directory
        self.binary = binary.resolve(strict=True)
        self.python = python.resolve(strict=True)
        self.provider = provider
        self.state: dict = {}

    def save(self, event: str, **details: object) -> None:
        self.state["events"].append(
            {"seq": len(self.state["events"]) + 1, "event": event, **details}
        )
        _save(self.directory / "run.json", self.state)

    def invoke(self, *arguments: str) -> dict:
        with tempfile.TemporaryDirectory(prefix="deep3d-agent-cli-") as temporary:
            output = Path(temporary) / "stdout.json"
            try:
                with output.open("wb") as stream:
                    process = subprocess.run(
                        [str(self.binary), *arguments],
                        stdout=stream,
                        stderr=subprocess.DEVNULL,
                        timeout=150,
                        check=False,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                value = read_json(output, max_bytes=8 * 1024 * 1024)
            except subprocess.TimeoutExpired as exc:
                raise AgentError("KERNEL_TIMEOUT", "Native kernel command timed out") from exc
            except (OSError, CadError) as exc:
                raise AgentError(
                    "KERNEL_PROTOCOL", "Native kernel returned no valid result"
                ) from exc
            if process.returncode or value.get("status") != "pass":
                error = value.get("error", {})
                raise AgentError(
                    error.get("code", "KERNEL_FAILED"),
                    error.get("message", "Native kernel rejected the operation"),
                )
            return value["result"]

    def inspect(self, arguments: dict) -> dict:
        if arguments:
            raise AgentError("TOOL_INPUT_INVALID", "inspect_project takes no arguments")
        project = self.state["base"]["project"]
        self.state["inspected"] = True
        return {
            "project_id": project["project_id"],
            "head": self.state["base"]["head"],
            "parameters_mm": {
                p["name"]: p["value"]["value"] for p in project["parameters"].values()
            },
            "feature_count": len(project["features"]),
            "goal": self.state["goal"],
        }

    def expected_project(self) -> dict:
        project = copy.deepcopy(self.state["base"]["project"])
        changes = self.state["goal"]["set_parameters_mm"]
        for parameter in project["parameters"].values():
            if parameter["name"] in changes:
                parameter["value"]["value"] = changes[parameter["name"]]
        return project

    def check_candidate(self, path: Path) -> dict:
        shown = self.invoke("show", "--project", str(path))
        candidate = shown["snapshot"]
        if (
            candidate["project"] != self.expected_project()
            or candidate["revision"]["parent"] != self.state["base"]["revision"]["id"]
            or int(candidate["head"]["generation"])
            != int(self.state["base"]["head"]["generation"]) + 1
        ):
            raise AgentError("CANDIDATE_MISMATCH", "Candidate differs from the authorized edit")
        return shown

    def evaluate(self, arguments: dict, call_id: str) -> dict:
        if not self.state["inspected"]:
            raise AgentError("INSPECTION_REQUIRED", "Inspect the project before proposing changes")
        if (
            set(arguments) != {"changes", "rationale"}
            or not isinstance(arguments["rationale"], str)
            or len(arguments["rationale"]) > 512
            or not isinstance(arguments["changes"], list)
            or not 1 <= len(arguments["changes"]) <= 5
        ):
            raise AgentError("TOOL_INPUT_INVALID", "Expected a bounded changes list and rationale")
        changes = {}
        for change in arguments["changes"]:
            if (
                not isinstance(change, dict)
                or set(change) != {"name", "value_mm"}
                or not isinstance(change["name"], str)
                or change["name"] in changes
                or change["name"] not in PARAMETERS
            ):
                raise AgentError("TOOL_INPUT_INVALID", "Each supported parameter may appear once")
            changes[change["name"]] = _number(change["value_mm"])
        if changes != self.state["goal"]["set_parameters_mm"]:
            raise AgentError(
                "GOAL_MISMATCH", "Proposed edits must match the independent goal exactly"
            )
        attempt = next((a for a in self.state["attempts"] if a["call_id"] == call_id), None)
        if attempt is None:
            if len(self.state["attempts"]) >= self.state["max_attempts"]:
                raise AgentError("ATTEMPT_LIMIT", "Candidate attempt budget exhausted")
            number = len(self.state["attempts"]) + 1
            attempt = {
                "number": number,
                "call_id": call_id,
                "status": "preparing",
                "candidate": f"attempt-{number}.deep3d",
                "transaction": f"transaction-{number}.json",
            }
            parameters = self.state["base"]["project"]["parameters"].values()
            ids = {p["name"]: p["id"] for p in parameters}
            transaction = {
                "schema_version": "deep3d.transaction/1",
                "id": str(uuid4()),
                "base_revision": self.state["base"]["head"]["revision_id"],
                "expected_generation": self.state["base"]["head"]["generation"],
                "message": "Agent: verified parameter goal",
                "commands": [
                    {
                        "op": "parameter.set",
                        "id": ids[name],
                        "value": {"kind": "length_mm", "value": value},
                    }
                    for name, value in sorted(changes.items())
                ],
            }
            _save(self.directory / attempt["transaction"], transaction)
            self.state["attempts"].append(attempt)
            self.save("candidate_requested", attempt=number)
        if attempt["status"] == "failed":
            raise AgentError(attempt["error"]["code"], attempt["error"]["message"])
        path = self.directory / attempt["candidate"]
        if not path.exists():
            self.invoke(
                "candidate",
                "--project",
                self.state["project"],
                "--transaction",
                str(self.directory / attempt["transaction"]),
                "--out",
                str(path),
            )
        shown = self.check_candidate(path)
        if not shown["materializations"]:
            if self.state["geometry_jobs"] >= self.state["max_attempts"]:
                raise AgentError("ATTEMPT_LIMIT", "Geometry job budget exhausted")
            self.state["geometry_jobs"] += 1
            attempt["status"] = "evaluating"
            output = self.directory / f"evaluation-{uuid4().hex}"
            attempt["evaluation_output"] = output.name
            self.save("geometry_started", attempt=attempt["number"])
            try:
                self.invoke(
                    "evaluate",
                    "--project",
                    str(path),
                    "--python",
                    str(self.python),
                    "--out",
                    str(output),
                    "--timeout",
                    "120",
                )
            except AgentError as exc:
                attempt["status"] = "failed"
                attempt["error"] = {"code": exc.code, "message": exc.message}
                self.save("geometry_rejected", attempt=attempt["number"], code=exc.code)
                raise
            shown = self.check_candidate(path)
        materialization, record = shown["materializations"][0]
        attempt.update(
            status="verified",
            revision_id=shown["snapshot"]["revision"]["id"],
            materialization=materialization,
            step=record["files"]["flange.step"],
        )
        self.save(
            "candidate_verified",
            attempt=attempt["number"],
            revision_id=attempt["revision_id"],
            materialization=materialization,
        )
        return {
            "attempt": attempt["number"],
            "verified": True,
            "revision_id": attempt["revision_id"],
            "materialization": materialization,
            "checks": ["native_brep", "step_roundtrip", "goal", "preserved_engineering_data"],
        }

    def accept(self, arguments: dict) -> dict:
        number = arguments.get("attempt")
        if (
            set(arguments) != {"attempt"}
            or type(number) is not int
            or not 1 <= number <= len(self.state["attempts"])
        ):
            raise AgentError("TOOL_INPUT_INVALID", "Select an existing verified attempt")
        attempt = self.state["attempts"][number - 1]
        if attempt["status"] != "verified":
            raise AgentError("CANDIDATE_UNVERIFIED", "The selected candidate has not passed")
        self.check_candidate(self.directory / attempt["candidate"])
        self.state.update(status="adopting", selected_attempt=number)
        self.save("adoption_started", attempt=number)
        return self.deliver()

    def deliver(self) -> dict:
        attempt = self.state["attempts"][self.state["selected_attempt"] - 1]
        base = self.state["base"]["head"]
        adopted = self.invoke(
            "adopt",
            "--project",
            self.state["project"],
            "--candidate",
            str(self.directory / attempt["candidate"]),
            "--expected-revision",
            base["revision_id"],
            "--expected-generation",
            base["generation"],
            "--materialization",
            attempt["materialization"],
        )
        if adopted["revision"]["id"] != attempt["revision_id"]:
            raise AgentError("CANDIDATE_MISMATCH", "Adoption returned a different revision")
        self.state["status"] = "delivering"
        self.save("candidate_adopted", revision_id=attempt["revision_id"])
        delivery = self.directory / "delivery.step"
        if delivery.exists():
            existing = delivery.read_bytes()
            if (
                len(existing) != attempt["step"]["size_bytes"]
                or "sha256:" + hashlib.sha256(existing).hexdigest() != attempt["step"]["sha256"]
            ):
                delivery.rename(self.directory / f"damaged-delivery-{uuid4().hex}.step")
                self.save("damaged_delivery_preserved")
        if not delivery.exists():
            staged = self.directory / f"delivery-{uuid4().hex}.step"
            self.invoke(
                "export",
                "--project",
                self.state["project"],
                "--materialization",
                attempt["materialization"],
                "--out",
                str(staged),
            )
            staged_data = staged.read_bytes()
            if (
                len(staged_data) != attempt["step"]["size_bytes"]
                or "sha256:" + hashlib.sha256(staged_data).hexdigest() != attempt["step"]["sha256"]
            ):
                raise AgentError("DELIVERY_MISMATCH", "Export differs from verified STEP evidence")
            # The native export fsyncs its output; expose it only after hash validation.
            staged.replace(delivery)
        data = delivery.read_bytes()
        if (
            len(data) != attempt["step"]["size_bytes"]
            or "sha256:" + hashlib.sha256(data).hexdigest() != attempt["step"]["sha256"]
        ):
            raise AgentError("DELIVERY_MISMATCH", "Delivery differs from verified STEP evidence")
        result = {
            "schema_version": "deep3d.agent-result/1",
            "status": "completed",
            "run_id": self.state["run_id"],
            "revision_id": attempt["revision_id"],
            "materialization": attempt["materialization"],
            "delivery": str(delivery),
            "step_sha256": attempt["step"]["sha256"],
            "parameters_mm": self.state["goal"]["set_parameters_mm"],
            "industrial_qualification": "not_established",
        }
        _save(self.directory / "result.json", result)
        self.state.update(status="completed", result=result)
        self.save("completed")
        return result

    def dispatch(self, call: dict) -> dict:
        name, arguments = call["name"], call["input"]
        if name == "inspect_project":
            return self.inspect(arguments)
        if name == "evaluate_parameters":
            return self.evaluate(arguments, call["id"])
        if name == "accept_candidate":
            return self.accept(arguments)
        raise AgentError("TOOL_FORBIDDEN", "Tool is outside the CAD operator capability set")

    def loop(self) -> dict:
        if self.state["status"] == "completed":
            attempt = self.state["attempts"][self.state["selected_attempt"] - 1]
            delivery = self.directory / "delivery.step"
            data = delivery.read_bytes() if delivery.is_file() else b""
            if (
                len(data) == attempt["step"]["size_bytes"]
                and "sha256:" + hashlib.sha256(data).hexdigest() == attempt["step"]["sha256"]
            ):
                return self.state["result"]
            self.state["status"] = "delivering"
            self.save("delivery_recovery_requested")
            return self.deliver()
        if self.state["status"] in {"adopting", "delivering"}:
            return self.deliver()
        if self.state["status"] == "failed":
            failure = self.state["error"]
            raise AgentError(failure["code"], failure["message"])
        while True:
            # Persist the assistant response before any side effect. Resume drains pending calls.
            pending = self.state.get("pending", [])
            if pending:
                for call in pending:
                    if call["id"] in self.state["tool_results"]:
                        continue
                    try:
                        value = self.dispatch(call)
                        response = {
                            "type": "tool_result",
                            "tool_use_id": call["id"],
                            "content": json.dumps(value, ensure_ascii=False),
                        }
                    except AgentError as exc:
                        if self.state["status"] in {"adopting", "delivering"} or exc.code in {
                            "REVISION_CONFLICT",
                            "PROJECT_BUSY",
                            "ATTEMPT_LIMIT",
                        }:
                            raise
                        response = {
                            "type": "tool_result",
                            "tool_use_id": call["id"],
                            "is_error": True,
                            "content": json.dumps({"code": exc.code, "message": exc.message}),
                        }
                    self.state["tool_results"][call["id"]] = response
                    self.save("tool_completed", tool=call["name"], call_id=call["id"])
                    if self.state["status"] == "completed":
                        return self.state["result"]
                self.state["messages"].append(
                    {
                        "role": "user",
                        "content": [self.state["tool_results"][call["id"]] for call in pending],
                    }
                )
                self.state["pending"] = []
                self.save("tool_results_recorded")
            if self.state["turns"] >= self.state["max_turns"]:
                raise AgentError("TURN_LIMIT", "Model turn budget exhausted")
            self.state["turns"] += 1
            self.state["status"] = "running"
            self.save("model_requested", turn=self.state["turns"])
            try:
                response = self.provider.complete(SYSTEM, self.state["messages"], TOOLS)
            except Exception as exc:
                # Provider bodies/URLs/credentials must never enter the run log.
                code = getattr(exc, "code", "PROVIDER_UNAVAILABLE")
                if not isinstance(code, str) or not code.startswith("PROVIDER_"):
                    code = "PROVIDER_UNAVAILABLE"
                self.state.update(
                    status="waiting_provider",
                    error={"code": code, "message": "Model call failed; resume this run to retry"},
                )
                self.save("provider_failed", code=code)
                raise AgentError(code, "Model call failed; resume this run to retry") from None
            content = response.get("content") if isinstance(response, dict) else None
            try:
                bounded = len(json.dumps(content, allow_nan=False).encode()) <= 65536
            except (ValueError, TypeError, RecursionError):
                bounded = False
            if (
                not bounded
                or not isinstance(content, list)
                or len(content) > 16
                or any(
                    not isinstance(item, dict)
                    or item.get("type") not in {"text", "tool_use"}
                    or (item["type"] == "text" and not isinstance(item.get("text"), str))
                    for item in content
                )
            ):
                raise AgentError("PROVIDER_PROTOCOL", "Expected bounded tool-use content")
            calls = [item for item in content if item.get("type") == "tool_use"]
            seen = set(self.state["tool_results"])
            for call in calls:
                if (
                    not isinstance(call.get("id"), str)
                    or not 1 <= len(call["id"]) <= 256
                    or call["id"] in seen
                    or not isinstance(call.get("name"), str)
                    or not 1 <= len(call["name"]) <= 64
                    or not isinstance(call.get("input"), dict)
                ):
                    raise AgentError("PROVIDER_PROTOCOL", "Invalid or duplicate tool call")
                seen.add(call["id"])
            if not calls or response.get("stop_reason") != "tool_use":
                raise AgentError("AGENT_INCOMPLETE", "Model ended before verified delivery")
            self.state["messages"].append({"role": "assistant", "content": content})
            self.state["pending"] = calls
            self.save("model_responded", tools=[call["name"] for call in calls])

    def execute(self) -> dict:
        try:
            return self.loop()
        except AgentError as exc:
            recoverable_adoption = self.state["status"] in {
                "adopting",
                "delivering",
            } and exc.code not in {
                "REVISION_CONFLICT",
                "CANDIDATE_INVALID",
                "IDEMPOTENCY_CONFLICT",
                "MATERIALIZATION_NOT_FOUND",
                "CANDIDATE_MISMATCH",
            }
            if recoverable_adoption:
                # A lost CLI reply can follow a committed atomic replacement. Do not
                # return control to the model: resume the identical idempotent adoption.
                self.state["error"] = {"code": exc.code, "message": exc.message}
                self.save("delivery_interrupted", code=exc.code)
            elif self.state["status"] != "waiting_provider":
                self.state.update(status="failed", error={"code": exc.code, "message": exc.message})
                self.save("failed", code=exc.code)
            raise


def run_agent(
    *,
    project: Path,
    request: str,
    goal: dict,
    run_dir: Path,
    binary: Path,
    python: Path,
    provider: object,
    max_turns: int = 8,
    max_attempts: int = 3,
) -> dict:
    goal = validate_goal(goal)
    if not isinstance(request, str) or not 1 <= len(request) <= 4096:
        raise AgentError("REQUEST_INVALID", "Request must contain 1 to 4096 characters")
    if type(max_turns) is not int or not 1 <= max_turns <= 12:
        raise AgentError("BUDGET_INVALID", "Model budget must be 1 to 12 turns")
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        raise AgentError("BUDGET_INVALID", "Geometry budget must be 1 to 3 attempts")
    directory = run_dir.resolve()
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise AgentError("OUTPUT_EXISTS", "Run directory already exists; use resume") from exc
    with _run_lock(directory):
        runtime = _Runtime(directory, binary, python, provider)
        source = project.resolve(strict=True)
        base = runtime.invoke("show", "--project", str(source))["snapshot"]
        parameters = {p["name"]: p for p in base["project"]["parameters"].values()}
        for name in goal["set_parameters_mm"]:
            if name not in parameters or parameters[name]["value"]["kind"] != "length_mm":
                raise AgentError("GOAL_INVALID", "Goal refers to an unsupported project parameter")
        runtime.state = {
            "schema_version": "deep3d.agent-run/1",
            "run_id": str(uuid4()),
            "project": str(source),
            "request": request,
            "goal": goal,
            "goal_sha256": _digest(goal),
            "base": base,
            "model": getattr(provider, "model", "scripted"),
            "status": "running",
            "max_turns": max_turns,
            "max_attempts": max_attempts,
            "turns": 0,
            "geometry_jobs": 0,
            "inspected": False,
            "attempts": [],
            "events": [],
            "tool_results": {},
            "pending": [],
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(
                        {"request": request, "independent_goal": goal}, ensure_ascii=False
                    ),
                }
            ],
        }
        runtime.save("created")
        return runtime.execute()


def resume_agent(*, run_dir: Path, binary: Path, python: Path, provider: object) -> dict:
    directory = run_dir.resolve(strict=True)
    with _run_lock(directory):
        runtime = _Runtime(directory, binary, python, provider)
        runtime.state = read_json(directory / "run.json", max_bytes=8 * 1024 * 1024)
        if (
            runtime.state.get("schema_version") != "deep3d.agent-run/1"
            or _digest(validate_goal(runtime.state["goal"])) != runtime.state["goal_sha256"]
        ):
            raise AgentError("RUN_CORRUPT", "Run contract or goal digest is invalid")
        if runtime.state["model"] != getattr(provider, "model", "scripted"):
            raise AgentError("MODEL_MISMATCH", "Resume must use the original model")
        runtime.save("resumed")
        return runtime.execute()
