"""Frozen-input CAD execution with independent lease renewal and durable result outbox."""

from __future__ import annotations

import hashlib
import math
import os
import re
import sys
import threading
import time
import unicodedata
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from deep3d_cad.contracts import CadError
from deep3d_cad.jsonio import read_json, write_json

from .client import RuntimeClient, WorkerError
from .process import ProcessTree

_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HASH = re.compile(r"^sha256:[a-f0-9]{64}$")
_TRANSIENT_NATIVE = {"PROJECT_BUSY", "IO_ERROR", "PROJECT_IO", "WORKER_TIMEOUT"}
_AUTHORITY_ERRORS = {
    "STALE_EXECUTION_LEASE",
    "STALE_CONTROL",
    "RUN_NOT_RUNNING",
    "LEASE_EXPIRED",
    "LEASE_STALE",
    "TASK_ALREADY_COMPLETED",
}


def _uuid(value: object) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value and UUID(value).int != 0
    except ValueError:
        return False


def _parameter_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= 256
        and not any(unicodedata.category(c) == "Cc" for c in value)
    )


def _acceptance(value: object) -> None:
    if not isinstance(value, dict) or not {"schema_version", "nodes"}.issubset(value):
        raise ValueError("Invalid mechanical acceptance")
    if set(value) - {"schema_version", "nodes", "assembly", "max_interference_volume_mm3"}:
        raise ValueError("Unknown mechanical acceptance member")
    if value["schema_version"] != "deep3d.mechanical-acceptance/1":
        raise ValueError("Unsupported mechanical acceptance")
    nodes = value["nodes"]
    if not isinstance(nodes, dict) or not 1 <= len(nodes) <= 64 or not all(_uuid(k) for k in nodes):
        raise ValueError("Invalid mechanical acceptance nodes")
    targets = list(nodes.values())
    if value.get("assembly") is not None:
        targets.append(value["assembly"])
    for target in targets:
        if (
            not isinstance(target, dict)
            or "solid_count" not in target
            or set(target) - {"solid_count", "volume_mm3", "bounds_mm"}
            or type(target["solid_count"]) is not int
            or not 1 <= target["solid_count"] <= 4096
            or target.get("volume_mm3") is None
            and target.get("bounds_mm") is None
        ):
            raise ValueError("Invalid independent node target")
        volume = target.get("volume_mm3")
        if volume is not None and (
            type(volume) not in (int, float) or not math.isfinite(volume) or volume <= 0
        ):
            raise ValueError("Invalid expected volume")
        bounds = target.get("bounds_mm")
        if bounds is not None and (
            not isinstance(bounds, list)
            or len(bounds) != 6
            or any(
                type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 10000
                for v in bounds
            )
            or any(bounds[i] >= bounds[i + 3] for i in range(3))
        ):
            raise ValueError("Invalid expected bounds")
    limit = value.get("max_interference_volume_mm3")
    if limit is not None and (
        type(limit) not in (int, float) or not math.isfinite(limit) or not 0 <= limit <= 1e12
    ):
        raise ValueError("Invalid assembly interference limit")


def validate_lease(lease: object) -> dict:
    try:
        if not isinstance(lease, dict) or set(lease) - {"acceptance"} != {
            "token",
            "source",
            "changes_mm",
            "lease_until_unix_ms",
            "deadline_unix_ms",
            "heartbeat_after_seconds",
        }:
            raise ValueError
        token, source, changes = lease["token"], lease["source"], lease["changes_mm"]
        if (
            not isinstance(token, dict)
            or set(token)
            != {"run_id", "task_key", "attempt_id", "worker_id", "execution_epoch", "control_epoch"}
            or not _uuid(token["run_id"])
            or not _uuid(token["attempt_id"])
            or not isinstance(token["task_key"], str)
            or not _KEY.fullmatch(token["task_key"])
            or not isinstance(token["worker_id"], str)
            or not 1 <= len(token["worker_id"]) <= 128
            or any(
                type(token[k]) is not int or token[k] < 0
                for k in ("execution_epoch", "control_epoch")
            )
        ):
            raise ValueError
        if (
            not isinstance(source, dict)
            or set(source) != {"path", "sha256", "head"}
            or not isinstance(source["path"], str)
            or not 1 <= len(source["path"]) <= 512
            or not isinstance(source["sha256"], str)
            or not _HASH.fullmatch(source["sha256"])
            or not isinstance(source["head"], dict)
            or set(source["head"]) != {"revision_id", "generation"}
            or not _uuid(source["head"]["revision_id"])
            or not isinstance(source["head"]["generation"], str)
            or not re.fullmatch(r"0|[1-9][0-9]{0,19}", source["head"]["generation"])
            or int(source["head"]["generation"]) > 2**64 - 1
        ):
            raise ValueError
        if (
            not isinstance(changes, dict)
            or not 1 <= len(changes) <= 64
            or not all(_parameter_name(name) for name in changes)
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in changes.values())
        ):
            raise ValueError
        if lease.get("acceptance") is not None:
            _acceptance(lease["acceptance"])
        if (
            any(
                type(lease[k]) is not int or lease[k] <= 0
                for k in ("lease_until_unix_ms", "deadline_unix_ms")
            )
            or lease["lease_until_unix_ms"] > lease["deadline_unix_ms"]
            or type(lease["heartbeat_after_seconds"]) not in (int, float)
            or not math.isfinite(lease["heartbeat_after_seconds"])
            or not 0 < lease["heartbeat_after_seconds"] <= 300
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError, OverflowError):
        raise WorkerError("RUNTIME_PROTOCOL") from None
    return lease


def _relative(root: Path, value: str, *, exists: bool) -> Path:
    parts = PurePosixPath(value)
    if (
        "\\" in value
        or ":" in value
        or parts.is_absolute()
        or any(p in {".", "..", ""} for p in value.split("/"))
    ):
        raise WorkerError("ARTIFACT_PATH_INVALID")
    try:
        path = root.joinpath(*parts.parts).resolve(strict=exists)
        if not path.is_relative_to(root) or path == root:
            raise WorkerError("ARTIFACT_PATH_INVALID")
        return path
    except OSError:
        raise WorkerError("ARTIFACT_UNREADABLE", retryable=True) from None


def _file_digest(path: Path) -> str:
    if path.stat().st_size > 256 * 1024 * 1024:
        raise WorkerError("ARTIFACT_TOO_LARGE")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


class LeaseKeeper:
    def __init__(self, client: RuntimeClient, lease: dict, force: threading.Event):
        self.client, self.lease, self.force = client, validate_lease(lease), force
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.completing = threading.Event()
        self._lock = threading.Lock()
        self._error = "LEASE_EXPIRED"
        wall, clock = time.time(), time.monotonic()
        self.deadline = clock + lease["deadline_unix_ms"] / 1000 - wall
        self.valid_until = clock + lease["lease_until_unix_ms"] / 1000 - wall
        self.thread = threading.Thread(
            target=self._heartbeat, daemon=True, name="deep3d-lease-heartbeat"
        )

    def __enter__(self):
        self.guard()
        self.thread.start()
        return self

    def guard(self) -> None:
        if self.force.is_set():
            raise WorkerError("WORKER_SHUTDOWN", retryable=True)
        with self._lock:
            if self.lost.is_set():
                raise WorkerError(self._error)
            if time.monotonic() >= min(self.valid_until, self.deadline):
                self._error = "LEASE_EXPIRED"
                self.lost.set()
                raise WorkerError("LEASE_EXPIRED")

    def _heartbeat(self) -> None:
        interval = self.lease["heartbeat_after_seconds"]
        while not self.stop.wait(max(0.05, interval)):
            try:
                response = validate_lease(self.client.post("heartbeat", self.lease["token"]))
                if (
                    response["token"] != self.lease["token"]
                    or response["source"] != self.lease["source"]
                    or response["changes_mm"] != self.lease["changes_mm"]
                    or response.get("acceptance") != self.lease.get("acceptance")
                    or response["deadline_unix_ms"] != self.lease["deadline_unix_ms"]
                ):
                    raise WorkerError("RUNTIME_PROTOCOL")
                with self._lock:
                    self.valid_until = time.monotonic() + (
                        response["lease_until_unix_ms"] / 1000 - time.time()
                    )
                interval = response["heartbeat_after_seconds"]
            except WorkerError as exc:
                if self.completing.is_set():
                    # Acceptance may have committed while its response was lost.
                    # Complete must retry its receipt, even when heartbeat is now stale.
                    return
                if exc.retryable:
                    interval = min(0.5, self.lease["heartbeat_after_seconds"])
                    continue
                with self._lock:
                    self._error = exc.code
                    self.lost.set()
                return

    def __exit__(self, *args):
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=getattr(self.client, "timeout", 5) + 1)


class TaskRunner:
    def __init__(self, client: RuntimeClient, root: Path, binary: Path, python: Path | None = None):
        self.client = client
        self.root = root.resolve(strict=True)
        self.binary = binary.resolve(strict=True)
        self.python = (python or Path(sys.executable)).resolve(strict=True)
        self.outbox = self.root / "runs" / ".worker-outbox"
        self.outbox.mkdir(parents=True, exist_ok=True)

    def invoke(self, arguments: list[str], keeper: LeaseKeeper, directory: Path) -> dict:
        keeper.guard()
        name = uuid4().hex
        output, error = directory / f"cli-{name}.json", directory / f"cli-{name}.stderr.log"
        try:
            with output.open("xb") as stdout, error.open("xb") as stderr:
                with ProcessTree(
                    [str(self.binary), *arguments], stdout=stdout, stderr=stderr
                ) as tree:
                    while tree.process.poll() is None:
                        keeper.guard()
                        if output.stat().st_size + error.stat().st_size > 16 * 1024 * 1024:
                            raise WorkerError("WORKER_LOG_LIMIT")
                        time.sleep(0.05)
                    code = tree.process.returncode
            payload = read_json(output, max_bytes=8 * 1024 * 1024)
            if code or payload.get("status") != "pass":
                result = payload.get("error", {})
                failure = result.get("code", "NATIVE_REJECTED")
                if not isinstance(failure, str) or not re.fullmatch(
                    r"[A-Z][A-Z0-9_]{0,63}", failure
                ):
                    failure = "NATIVE_REJECTED"
                raise WorkerError(failure, retryable=failure in _TRANSIENT_NATIVE)
            keeper.guard()
            return payload["result"]
        except (OSError, CadError, KeyError, TypeError):
            raise WorkerError("NATIVE_PROTOCOL", retryable=True) from None

    def execute(self, lease: dict, force: threading.Event | None = None) -> object:
        lease = validate_lease(lease)
        force = force or threading.Event()
        token = lease["token"]
        relative = (
            f"runs/{token['run_id']}/tasks/{token['task_key']}/attempts/{token['attempt_id']}"
        )
        directory = _relative(self.root, relative, exists=False)
        directory.mkdir(parents=True, exist_ok=True)
        immutable = {
            key: lease[key] for key in ("token", "source", "changes_mm", "deadline_unix_ms")
        }
        if lease.get("acceptance") is not None:
            immutable["acceptance"] = lease["acceptance"]
        journal = directory / "lease.json"
        if journal.exists() and read_json(journal) != immutable:
            raise WorkerError("ATTEMPT_WORKSPACE_CONFLICT")
        write_json(journal, immutable)
        pending = self.outbox / f"{token['attempt_id']}.pending.json"
        try:
            with LeaseKeeper(self.client, lease, force) as keeper:
                frozen_source = _relative(self.root, lease["source"]["path"], exists=True)
                if _file_digest(frozen_source) != lease["source"]["sha256"]:
                    raise WorkerError("INPUT_HASH_MISMATCH")
                # Native candidate preparation creates an adjacent OS lock file.
                # Copy frozen CAS bytes into the writable attempt workspace so a
                # worker never needs write permission on coordinator-owned objects.
                source = directory / "input.deep3d"
                if not source.exists():
                    temporary = directory / f"input-{uuid4().hex}.tmp"
                    with frozen_source.open("rb") as incoming, temporary.open("xb") as copied:
                        while chunk := incoming.read(1024 * 1024):
                            copied.write(chunk)
                        copied.flush()
                        os.fsync(copied.fileno())
                    try:
                        os.link(temporary, source)
                    except FileExistsError:
                        pass
                    finally:
                        temporary.unlink(missing_ok=True)
                if _file_digest(source) != lease["source"]["sha256"]:
                    raise WorkerError("INPUT_HASH_MISMATCH")
                shown = self.invoke(["show", "--project", str(source)], keeper, directory)
                snapshot = shown["snapshot"]
                if snapshot["head"] != lease["source"]["head"]:
                    raise WorkerError("INPUT_HEAD_MISMATCH")
                parameters = {p["name"]: p for p in snapshot["project"]["parameters"].values()}
                commands = []
                for name, value in sorted(lease["changes_mm"].items()):
                    if name not in parameters or parameters[name]["value"]["kind"] != "length_mm":
                        raise WorkerError("TASK_PARAMETER_INVALID")
                    commands.append(
                        {
                            "op": "parameter.set",
                            "id": parameters[name]["id"],
                            "value": {"kind": "length_mm", "value": value},
                        }
                    )
                if lease.get("acceptance") is not None:
                    if snapshot["project"].get("flange") is not None:
                        raise WorkerError("TASK_ACCEPTANCE_INVALID")
                    commands.append({"op": "acceptance.set", "acceptance": lease["acceptance"]})
                transaction = {
                    "schema_version": "deep3d.transaction/1",
                    "id": token["attempt_id"],
                    "base_revision": snapshot["head"]["revision_id"],
                    "expected_generation": snapshot["head"]["generation"],
                    "message": "Durable task parameter edit",
                    "commands": commands,
                }
                transaction_path, candidate = (
                    directory / "transaction.json",
                    directory / "candidate.deep3d",
                )
                if transaction_path.exists() and read_json(transaction_path) != transaction:
                    raise WorkerError("ATTEMPT_WORKSPACE_CONFLICT")
                write_json(transaction_path, transaction)
                if not candidate.exists():
                    self.invoke(
                        [
                            "candidate",
                            "--project",
                            str(source),
                            "--transaction",
                            str(transaction_path),
                            "--out",
                            str(candidate),
                        ],
                        keeper,
                        directory,
                    )
                shown = self.invoke(["show", "--project", str(candidate)], keeper, directory)
                current = shown["snapshot"]
                if (
                    current["revision"]["transaction_id"] != token["attempt_id"]
                    or current["revision"]["parent"] != snapshot["revision"]["id"]
                ):
                    raise WorkerError("ATTEMPT_WORKSPACE_CONFLICT")
                if shown["materializations"]:
                    materialization = shown["materializations"][0][0]
                else:
                    timeout = max(1, min(600, math.floor(keeper.deadline - time.monotonic())))
                    output = directory / f"evaluation-{uuid4().hex}"
                    evaluated = self.invoke(
                        [
                            "evaluate",
                            "--project",
                            str(candidate),
                            "--python",
                            str(self.python),
                            "--out",
                            str(output),
                            "--timeout",
                            str(timeout),
                            "--cache-dir",
                            str(self.root / "runs" / ".action-cache"),
                        ],
                        keeper,
                        directory,
                    )
                    materialization = evaluated["materialization"]
                if (
                    _file_digest(source) != lease["source"]["sha256"]
                    or _file_digest(frozen_source) != lease["source"]["sha256"]
                ):
                    raise WorkerError("INPUT_HASH_MISMATCH")
                request = {"lease": token, "materialization": materialization}
                write_json(
                    pending, {"schema_version": "deep3d.worker-outbox/1", "request": request}
                )
                keeper.completing.set()
                return self.send_pending(pending, force)
        except WorkerError as exc:
            if not pending.exists() and exc.code not in _AUTHORITY_ERRORS:
                try:
                    self.client.post(
                        "fail", {"lease": token, "code": exc.code, "retryable": exc.retryable}
                    )
                except WorkerError:
                    pass  # The expired lease recovers without this best-effort notification.
            raise
        except (OSError, CadError, KeyError, TypeError):
            raise WorkerError("WORKER_IO", retryable=True) from None

    def send_pending(self, path: Path, force: threading.Event) -> object:
        done = path.with_name(path.name.replace(".pending.json", ".done.json"))
        try:
            document = read_json(path)
        except CadError:
            if done.exists():
                return read_json(done)["result"]
            raise
        if document.get("schema_version") != "deep3d.worker-outbox/1":
            raise WorkerError("OUTBOX_INVALID")
        request = document["request"]
        delay = 0.25
        while not force.is_set():
            try:
                result = self.client.post("complete", request)
                write_json(done, {**document, "result": result})
                path.unlink(missing_ok=True)
                return result
            except WorkerError as exc:
                if not exc.retryable:
                    if exc.code not in {"WORKER_AUTH", "RUNTIME_PROTOCOL", "RUNTIME_REDIRECT"}:
                        write_json(
                            path.with_name(path.name.replace(".pending.json", ".rejected.json")),
                            {**document, "error": exc.code},
                        )
                        path.unlink(missing_ok=True)
                    raise
                force.wait(delay)
                delay = min(5, delay * 2)
        raise WorkerError("WORKER_SHUTDOWN", retryable=True)

    def replay_pending(self, stop: threading.Event) -> int:
        count = 0
        for path in sorted(self.outbox.glob("*.pending.json"))[:100]:
            if stop.is_set():
                break
            try:
                self.send_pending(path, stop)
                count += 1
            except WorkerError as exc:
                if exc.code == "WORKER_AUTH":
                    raise
            except (OSError, CadError, KeyError, TypeError):
                # Leave an invalid local outbox for operator inspection.
                continue
        return count
