"""Transport, ownership-loss and uncertain-completion boundaries of persistent workers."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from deep3d_worker.cli import WorkerPool
from deep3d_worker.client import RuntimeClient, WorkerError
from deep3d_worker.runner import LeaseKeeper, TaskRunner, validate_lease

TOKEN = "unit-test-worker-credential"


@contextmanager
def api_server(callback):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
            }
            requests.append(request)
            status, body, headers = callback(request)
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_runtime_transport_only_calls_worker_capabilities_and_authenticates():
    with api_server(lambda _: (200, {"status": "pass", "result": None}, {})) as (url, requests):
        client = RuntimeClient(url, TOKEN)
        for action in ("claim", "heartbeat", "complete", "fail"):
            assert client.post(action, {"identity": "fixed"}) is None
        with pytest.raises(WorkerError, match="WORKER_CAPABILITY"):
            client.post("control", {})
    assert [request["path"] for request in requests] == [
        "/v1/worker/claim",
        "/v1/worker/heartbeat",
        "/v1/worker/complete",
        "/v1/worker/fail",
    ]
    assert all(request["auth"] == "Bearer " + TOKEN for request in requests)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:password@example.com",
        "https://example.com?key=secret",
        "https://example.com#fragment",
        "https://example.com/v1",
        "file:///tmp/service",
        "https://example.com:99999",
        "https://example.com\n",
    ],
)
def test_runtime_rejects_unsafe_or_ambiguous_endpoint(url):
    with pytest.raises(WorkerError, match="WORKER_CONFIG"):
        RuntimeClient(url, TOKEN)


def test_runtime_redirect_does_not_forward_credentials():
    with api_server(lambda _: (200, {"status": "pass", "result": None}, {})) as (other, forwarded):
        with api_server(
            lambda _: (307, b"private error " + TOKEN.encode(), {"Location": other})
        ) as (url, requests):
            with pytest.raises(WorkerError) as failure:
                RuntimeClient(url, TOKEN).post("claim", {})
    assert failure.value.code == "RUNTIME_REDIRECT"
    assert TOKEN not in str(failure.value)
    assert len(requests) == 1 and not forwarded


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (403, "WORKER_AUTH", False),
        (429, "DB_BUSY", True),
        (503, "DB_BUSY", True),
        (409, "STALE_EXECUTION_LEASE", False),
        (422, "INPUT_INVALID", False),
    ],
)
def test_runtime_error_classification_and_redaction(status, code, retryable):
    with api_server(
        lambda _: (status, {"status": "error", "error": {"code": code, "message": TOKEN}}, {})
    ) as (url, _):
        with pytest.raises(WorkerError) as failure:
            RuntimeClient(url, TOKEN).post("claim", {})
    assert failure.value.code == code
    assert failure.value.retryable is retryable
    assert TOKEN not in str(failure.value)


@pytest.mark.parametrize(
    "body",
    [
        b'{"status":"pass","result":null,"result":1}',
        b'{"status":"pass","result":1e999}',
        b'{"status":"pass","result":NaN}',
        b'{"status":"pass"}',
        b"null",
        b"[1,2]",
    ],
)
def test_runtime_rejects_malformed_envelopes(body):
    with api_server(lambda _: (200, body, {})) as (url, _):
        with pytest.raises(WorkerError, match="RUNTIME_PROTOCOL"):
            RuntimeClient(url, TOKEN).post("claim", {})


def make_lease(*, path="runs/input.deep3d", sha256=None, lease_seconds=5):
    now = int(time.time() * 1000)
    return {
        "token": {
            "run_id": str(uuid4()),
            "task_key": "part-a",
            "attempt_id": str(uuid4()),
            "worker_id": "test-worker-1",
            "execution_epoch": 1,
            "control_epoch": 1,
        },
        "source": {
            "path": path,
            "sha256": sha256 or "sha256:" + "a" * 64,
            "head": {"revision_id": str(uuid4()), "generation": "0"},
        },
        "changes_mm": {"thickness_mm": 16},
        "lease_until_unix_ms": now + lease_seconds * 1000,
        "deadline_unix_ms": now + 60000,
        "heartbeat_after_seconds": 0.05,
    }


def mechanical_goal(node=None):
    return {
        "schema_version": "deep3d.mechanical-acceptance/1",
        "nodes": {
            node or str(uuid4()): {
                "solid_count": 1,
                "volume_mm3": 1200,
                "bounds_mm": [0, 0, 0, 24, 10, 5],
            }
        },
    }


def test_generic_length_parameter_names_and_optional_frozen_goal_are_supported():
    lease = make_lease()
    lease["changes_mm"] = {"block width mm": 24, "offset_x_mm": -3}
    lease["acceptance"] = mechanical_goal()
    assert validate_lease(lease) == lease
    lease["acceptance"] = None
    assert validate_lease(lease) == lease


@pytest.mark.parametrize(
    "mutate",
    [
        lambda x: x.update(changes_mm={"bad\nparameter": 24}),
        lambda x: x.update(changes_mm={f"p{i}": i for i in range(65)}),
        lambda x: x["acceptance"].update(nodes={}),
        lambda x: x["acceptance"].update(code="arbitrary Python"),
        lambda x: x["acceptance"]["nodes"].update(
            {"../../outside": {"solid_count": 1, "volume_mm3": 1}}
        ),
        lambda x: next(iter(x["acceptance"]["nodes"].values())).update(solid_count=True),
        lambda x: next(iter(x["acceptance"]["nodes"].values())).update(volume_mm3=float("nan")),
        lambda x: next(iter(x["acceptance"]["nodes"].values())).update(
            bounds_mm=[0, 0, 0, -1, 1, 1]
        ),
    ],
)
def test_generic_runtime_goal_remains_strict_and_bounded(mutate):
    lease = make_lease()
    lease["acceptance"] = mechanical_goal()
    mutate(lease)
    with pytest.raises(WorkerError, match="RUNTIME_PROTOCOL"):
        validate_lease(lease)


@pytest.mark.parametrize("parameter_kind", ["length_mm", "unitless", "missing"])
def test_worker_generic_goal_and_parameter_edits_form_one_bound_transaction(
    tmp_path, parameter_kind
):
    # This test isolates the transaction adapter; geometry is exercised separately.
    source = tmp_path / "source.deep3d"
    source.write_bytes(b"frozen native-input transport fixture")
    lease = make_lease(
        path=source.name, sha256="sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    )
    lease["changes_mm"] = {"block_width_mm": 24}
    lease["acceptance"] = mechanical_goal()
    parameter = {
        "id": str(uuid4()),
        "name": "block_width_mm",
        "value": {"kind": parameter_kind, "value": 20},
    }
    parameters = {} if parameter_kind == "missing" else {parameter["id"]: parameter}
    calls, transactions = [], []

    class Client:
        timeout = 0.1

        def post(self, action, body):
            calls.append((action, copy.deepcopy(body)))
            if action == "heartbeat":
                return copy.deepcopy(lease)
            return {"receipt": "accepted"}

    class ContractRunner(TaskRunner):
        def invoke(self, arguments, keeper, directory):
            if arguments[0] == "show":
                if Path(arguments[-1]).name == "candidate.deep3d":
                    return {
                        "snapshot": {
                            "revision": {
                                "id": str(uuid4()),
                                "parent": lease["source"]["head"]["revision_id"],
                                "transaction_id": lease["token"]["attempt_id"],
                            }
                        },
                        "materializations": [],
                    }
                return {
                    "snapshot": {
                        "head": lease["source"]["head"],
                        "revision": {"id": lease["source"]["head"]["revision_id"]},
                        "project": {"parameters": parameters, "acceptance": None},
                    }
                }
            if arguments[0] == "candidate":
                transaction = json.loads(
                    Path(arguments[arguments.index("--transaction") + 1]).read_text()
                )
                transactions.append(transaction)
                Path(arguments[-1]).write_bytes(b"candidate transport fixture")
                return {}
            assert arguments[0] == "evaluate"
            return {"materialization": "sha256:" + "b" * 64}

    runner = ContractRunner(Client(), tmp_path, Path(sys.executable))
    original_goal = copy.deepcopy(lease["acceptance"])
    if parameter_kind != "length_mm":
        with pytest.raises(WorkerError, match="TASK_PARAMETER_INVALID"):
            runner.execute(lease)
        assert not transactions
        assert calls[-1][0] == "fail"
    else:
        assert runner.execute(lease) == {"receipt": "accepted"}
        commands = transactions[0]["commands"]
        assert commands == [
            {
                "op": "parameter.set",
                "id": parameter["id"],
                "value": {"kind": "length_mm", "value": 24},
            },
            {"op": "acceptance.set", "acceptance": original_goal},
        ]
        assert transactions[0]["id"] == lease["token"]["attempt_id"]
        assert lease["acceptance"] == original_goal


def test_changed_goal_in_heartbeat_revokes_local_execution():
    lease = make_lease()
    lease["acceptance"] = mechanical_goal()
    response = copy.deepcopy(lease)
    next(iter(response["acceptance"]["nodes"].values()))["volume_mm3"] = 1

    class Client:
        timeout = 0.1

        def post(self, action, body):
            return response

    with LeaseKeeper(Client(), lease, threading.Event()) as keeper:
        assert keeper.lost.wait(1)
        with pytest.raises(WorkerError, match="RUNTIME_PROTOCOL"):
            keeper.guard()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda x: x["token"].update(attempt_id="../../escape"),
        lambda x: x["token"].update(task_key="../escape"),
        lambda x: x["token"].update(control_epoch=True),
        lambda x: x["changes_mm"].update(thickness_mm=True),
        lambda x: x["changes_mm"].update(thickness_mm=float("nan")),
        lambda x: x["source"]["head"].update(generation="01"),
        lambda x: x.update(heartbeat_after_seconds=float("inf")),
    ],
)
def test_lease_contract_rejects_unsafe_process_and_artifact_inputs(mutation):
    lease = make_lease()
    mutation(lease)
    with pytest.raises(WorkerError, match="RUNTIME_PROTOCOL"):
        validate_lease(lease)


def test_claim_transport_retry_keeps_exact_request_identity():
    calls = []
    pool = None

    class Client:
        def post(self, action, request):
            calls.append(copy.deepcopy(request))
            if len(calls) == 1:
                raise WorkerError("RUNTIME_UNAVAILABLE", retryable=True)
            pool.stop.set()
            return None

    pool = WorkerPool(SimpleNamespace(client=Client()), concurrency=1, worker_id="identity")
    pool._slot(0)
    assert len(calls) == 2 and calls[0] == calls[1]


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        status = wintypes.DWORD()
        try:
            return (
                bool(kernel.GetExitCodeProcess(handle, ctypes.byref(status)))
                and status.value == 259
            )
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        # A reparented zombie cannot execute, and waits for the container's PID 1.
        stat = Path(f"/proc/{pid}/stat")
        return not stat.exists() or stat.read_text().split()[2] != "Z"
    except ProcessLookupError:
        return False


@pytest.mark.parametrize("reason", ["revoked", "network_partition"])
def test_lost_lease_stops_native_command_and_its_descendant(tmp_path, reason):
    class Client:
        timeout = 0.2

        def post(self, action, request):
            if reason == "revoked" and not any(
                path.stat().st_size for path in tmp_path.glob("cli-*.json")
            ):
                return copy.deepcopy(lease)
            raise WorkerError(
                "STALE_EXECUTION_LEASE" if reason == "revoked" else "RUNTIME_UNAVAILABLE",
                retryable=reason == "network_partition",
            )

    lease = make_lease(lease_seconds=2)
    runner = TaskRunner(Client(), tmp_path, Path(sys.executable))
    code = (
        "import subprocess,sys,time,json; "
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
        "print(json.dumps({'pid':child.pid}),flush=True);time.sleep(60)"
    )
    started = time.monotonic()
    with LeaseKeeper(runner.client, lease, threading.Event()) as keeper:
        with pytest.raises(WorkerError) as failure:
            runner.invoke(["-u", "-c", code], keeper, tmp_path)
    assert failure.value.code in {"STALE_EXECUTION_LEASE", "LEASE_EXPIRED"}
    assert time.monotonic() - started < 5
    document = json.loads(next(tmp_path.glob("cli-*.json")).read_text())
    until = time.monotonic() + 3
    while _alive(document["pid"]) and time.monotonic() < until:
        time.sleep(0.05)
    assert not _alive(document["pid"])


def write_pending(runner, lease):
    pending = runner.outbox / f"{lease['token']['attempt_id']}.pending.json"
    request = {"lease": lease["token"], "materialization": "sha256:" + "b" * 64}
    pending.write_text(json.dumps({"schema_version": "deep3d.worker-outbox/1", "request": request}))
    return pending, request


def test_uncertain_completion_retries_same_request_and_persists_receipt(tmp_path):
    calls = []
    output = {"accepted": "durable-candidate"}

    class Client:
        def post(self, action, request):
            calls.append((action, copy.deepcopy(request)))
            if len(calls) == 1:
                raise WorkerError("RUNTIME_UNAVAILABLE", retryable=True)
            return output

    runner = TaskRunner(Client(), tmp_path, Path(sys.executable))
    pending, request = write_pending(runner, make_lease())
    assert runner.send_pending(pending, threading.Event()) == output
    assert calls == [("complete", request), ("complete", request)]
    assert not pending.exists()
    assert json.loads(next(runner.outbox.glob("*.done.json")).read_text())["result"] == output


def test_restart_replays_completion_outbox_after_uncertain_commit(tmp_path):
    stopped = threading.Event()
    calls = []

    class LostReply:
        def post(self, action, request):
            calls.append(copy.deepcopy(request))
            stopped.set()
            raise WorkerError("COMMIT_OUTCOME_UNKNOWN", retryable=True)

    runner = TaskRunner(LostReply(), tmp_path, Path(sys.executable))
    pending, request = write_pending(runner, make_lease())
    with pytest.raises(WorkerError, match="WORKER_SHUTDOWN"):
        runner.send_pending(pending, stopped)
    assert pending.exists()

    class Recovered:
        def post(self, action, body):
            assert action == "complete"
            calls.append(copy.deepcopy(body))
            return {"candidate": "original-committed-identity"}

    restored = TaskRunner(Recovered(), tmp_path, Path(sys.executable))
    assert restored.replay_pending(threading.Event()) == 1
    assert calls == [request, request]
    assert not pending.exists()


def test_outbox_auth_failure_stays_pending_for_corrected_configuration(tmp_path):
    class Client:
        def post(self, action, body):
            raise WorkerError("WORKER_AUTH")

    runner = TaskRunner(Client(), tmp_path, Path(sys.executable))
    pending, _ = write_pending(runner, make_lease())
    with pytest.raises(WorkerError, match="WORKER_AUTH"):
        runner.send_pending(pending, threading.Event())
    assert pending.exists() and not list(runner.outbox.glob("*.rejected.json"))


def test_wrong_frozen_input_hash_rejected_before_starting_native_process(tmp_path):
    source = tmp_path / "input.deep3d"
    source.write_bytes(b"untrusted changed source")
    calls = []

    class Client:
        timeout = 0.1

        def post(self, action, body):
            calls.append((action, body))
            return None

    runner = TaskRunner(Client(), tmp_path, Path(sys.executable))
    with pytest.raises(WorkerError, match="INPUT_HASH_MISMATCH"):
        runner.execute(make_lease(path="input.deep3d"))
    assert [action for action, _ in calls] == ["fail"]
    assert calls[0][1]["retryable"] is False
    assert not list(tmp_path.rglob("cli-*.json"))


@pytest.mark.parametrize(
    "path", ["../outside", "C:/secret", "/absolute", "runs//x", "runs/./x", "runs\\x"]
)
def test_worker_cannot_open_artifact_outside_frozen_relative_root(tmp_path, path):
    class Client:
        timeout = 0.1

        def post(self, action, body):
            return None

    runner = TaskRunner(Client(), tmp_path, Path(sys.executable))
    with pytest.raises(WorkerError, match="ARTIFACT_PATH_INVALID"):
        runner.execute(make_lease(path=path))


def test_worker_executes_real_native_candidate_without_adopting_source(tmp_path):
    binary = (
        Path(__file__).resolve().parents[1]
        / "target/debug"
        / ("deep3d.exe" if os.name == "nt" else "deep3d")
    )
    if not binary.is_file():
        pytest.skip("Build the trusted Rust CLI for real CAD worker integration")
    source = tmp_path / "input.deep3d"
    created = subprocess.run(
        [str(binary), "create", "--project", str(source)], check=True, capture_output=True
    )
    snapshot = json.loads(created.stdout)["result"]
    original = source.read_bytes()
    frozen = tmp_path / "objects/frozen.deep3d"
    frozen.parent.mkdir()
    frozen.write_bytes(original)
    lease = make_lease(
        path="objects/frozen.deep3d",
        sha256="sha256:" + hashlib.sha256(original).hexdigest(),
        lease_seconds=30,
    )
    lease["source"]["head"] = snapshot["head"]
    lease["heartbeat_after_seconds"] = 0.2
    received = []

    class Client:
        timeout = 0.1

        def post(self, action, body):
            if action == "heartbeat":
                result = copy.deepcopy(lease)
                result["lease_until_unix_ms"] = min(
                    int(time.time() * 1000) + 30000, lease["deadline_unix_ms"]
                )
                return result
            assert action == "complete", body
            received.append(body)
            return {"materialization": body["materialization"]}

    runner = TaskRunner(Client(), tmp_path, binary)
    result = runner.execute(lease)
    assert source.read_bytes() == original
    assert frozen.read_bytes() == original
    assert not frozen.with_suffix(".deep3d.lock").exists()
    assert len(received) == 1
    assert result["materialization"].startswith("sha256:")
    candidate = next(tmp_path.glob("runs/*/tasks/*/attempts/*/candidate.deep3d"))
    assert (candidate.parent / "input.deep3d").read_bytes() == original
    shown = subprocess.run(
        [str(binary), "show", "--project", str(candidate)], check=True, capture_output=True
    )
    payload = json.loads(shown.stdout)["result"]
    assert payload["snapshot"]["revision"]["transaction_id"] == lease["token"]["attempt_id"]
    assert len(payload["materializations"]) == 1
