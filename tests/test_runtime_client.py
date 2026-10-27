"""Offline operator protocol tests; real service/CAD integration is tested separately."""

import hashlib
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest
from deep3d_runtime_client import ClientError, RuntimeClient
from deep3d_runtime_client import client as transport
from deep3d_runtime_client.cli import main

TOKEN = "operator-test-token-DO-NOT-PRINT-12345"
REVISION = str(uuid4())
HEAD = {"revision_id": REVISION, "generation": "0"}
TASK = {
    "key": "thicker",
    "depends_on": [],
    "source_task": None,
    "changes_mm": {"thickness_mm": 16},
    "max_attempts": 3,
    "timeout_seconds": 120,
}


@pytest.fixture
def server(monkeypatch):
    state = {
        "requests": [],
        "drop_run_reply": False,
        "status": 200,
        "override": None,
        "artifact": b"ISO-10303-21;\nEND-ISO-10303-21;\n",
        "bad_hash": False,
        "checkpoint": None,
        "checkpoint_seen": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state["requests"].append(
                {
                    "method": self.command,
                    "path": self.path,
                    "body": body,
                    "authorization": self.headers.get("Authorization"),
                    "content_type": self.headers.get("Content-Type"),
                }
            )
            headers = {}
            if self.path.endswith("/artifacts/native") and self.command == "POST":
                result = {
                    "project_id": str(uuid4()),
                    "source": {
                        "path": "objects/frozen.deep3d",
                        "head": HEAD,
                        "sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
                    },
                }
                if state["bad_hash"]:
                    result["source"]["sha256"] = "sha256:" + "0" * 64
            elif self.path == "/v1/runs":
                checkpoint = state["checkpoint"]
                state["checkpoint_seen"].append(checkpoint is not None and checkpoint.is_file())
                result = {"state": "running", "run_id": json.loads(body)["run_id"]}
                if state["drop_run_reply"]:
                    state["drop_run_reply"] = False
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
            elif "/tasks/" in self.path:
                result = None
                headers = {
                    "x-content-sha256": "sha256:" + hashlib.sha256(state["artifact"]).hexdigest(),
                    "x-spec-revision": REVISION,
                }
                if state["bad_hash"]:
                    headers["x-content-sha256"] = "sha256:" + "0" * 64
            elif "/events?" in self.path:
                result = [{"seq": 8, "kind": "task.accepted"}]
            else:
                result = {"state": "paused" if self.path.endswith("/control") else "running"}
            data = (
                state["artifact"]
                if headers
                else json.dumps({"status": "pass", "result": result}).encode()
            )
            if state["override"] is not None:
                data = state["override"]
            self.send_response(state["status"])
            if state["status"] in {301, 302, 307, 308}:
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/leaked")
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        do_GET = handle_request
        do_POST = handle_request

        def log_message(self, *args):
            pass

    instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=lambda: instance.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    state["url"] = f"http://127.0.0.1:{instance.server_port}"
    monkeypatch.setenv("DEEP3D_OPERATOR_TOKEN", TOKEN)
    yield state
    instance.shutdown()
    instance.server_close()
    thread.join(timeout=2)


def cli(server, *args):
    return main(["--api", server["url"], *map(str, args)])


def inputs(tmp_path):
    project, plan, checkpoint = (
        tmp_path / name for name in ("part.deep3d", "plan.json", "run.json")
    )
    project.write_bytes(b"native package fixture; service validates geometry in integration tests")
    plan.write_text(json.dumps({"schema_version": "deep3d.task-plan/1", "tasks": [TASK]}))
    return project, plan, checkpoint


def test_lost_submit_reply_reuses_checkpoint_without_reimport(server, tmp_path, capsys):
    project, plan, checkpoint = inputs(tmp_path)
    server.update(checkpoint=checkpoint, drop_run_reply=True)
    assert cli(server, "submit", "--project", project, "--plan", plan, "--out", checkpoint) == 2
    first = json.loads(capsys.readouterr().out)
    assert first["error"]["code"] == "CLIENT_UNAVAILABLE"
    saved = checkpoint.read_bytes()
    request = json.loads(saved)
    assert request["schema_version"] == "deep3d.submit-run/1"
    assert request["expected_head"] == HEAD
    assert request["tasks"] == [TASK]
    assert cli(server, "submit", "--request", checkpoint) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["result"]["run_id"] == request["run_id"]
    assert checkpoint.read_bytes() == saved
    runs = [r for r in server["requests"] if r["path"] == "/v1/runs"]
    imports = [r for r in server["requests"] if r["path"] == "/v1/artifacts/native"]
    assert len(imports) == 1 and len(runs) == 2
    assert runs[0]["body"] == runs[1]["body"]
    assert server["checkpoint_seen"] == [True, True]
    assert imports[0]["body"] == project.read_bytes()
    assert imports[0]["content_type"] == "application/octet-stream"
    assert all(r["authorization"] == f"Bearer {TOKEN}" for r in server["requests"])


def test_existing_checkpoint_never_overwrites_or_imports(server, tmp_path, capsys):
    project, plan, checkpoint = inputs(tmp_path)
    checkpoint.write_bytes(b"existing")
    assert cli(server, "submit", "--project", project, "--plan", plan, "--out", checkpoint) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "OUTPUT_EXISTS"
    assert checkpoint.read_bytes() == b"existing"
    assert not server["requests"]


def test_imported_bytes_must_match_before_creating_run(server, tmp_path, capsys):
    project, plan, checkpoint = inputs(tmp_path)
    server["bad_hash"] = True
    assert cli(server, "submit", "--project", project, "--plan", plan, "--out", checkpoint) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "CLIENT_PROTOCOL"
    assert not checkpoint.exists()
    assert len(server["requests"]) == 1


def test_status_and_events_are_read_only_with_cursor(server, capsys):
    run_id = str(uuid4())
    assert cli(server, "status", run_id) == 0
    assert cli(server, "events", run_id, "--after", "7", "--limit", "50") == 0
    outputs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert outputs[1]["result"][0]["seq"] == 8
    assert [r["method"] for r in server["requests"]] == ["GET", "GET"]
    assert server["requests"][1]["path"] == f"/v1/runs/{run_id}/events?after=7&limit=50"


def test_control_retries_use_exact_explicit_identity(server, capsys):
    run_id, operation = str(uuid4()), str(uuid4())
    for _ in range(2):
        assert (
            cli(
                server,
                "control",
                run_id,
                "--action",
                "pause",
                "--expected-epoch",
                "3",
                "--operation-id",
                operation,
            )
            == 0
        )
    capsys.readouterr()
    bodies = [r["body"] for r in server["requests"]]
    assert bodies[0] == bodies[1]
    assert json.loads(bodies[0]) == {
        "operation_id": operation,
        "expected_control_epoch": 3,
        "action": "pause",
    }
    with pytest.raises(SystemExit):
        cli(server, "control", run_id, "--action", "pause", "--expected-epoch", "3")
    assert len(server["requests"]) == 2


@pytest.mark.parametrize("status", [401, 403, 409, 503])
def test_service_error_code_preserved_but_body_credentials_and_urls_redacted(server, status):
    server["status"] = status
    server["override"] = json.dumps(
        {
            "status": "error",
            "error": {
                "code": "CONTROL_CONFLICT",
                "message": f"{TOKEN} https://private.example/secret",
            },
        }
    ).encode()
    with pytest.raises(ClientError) as error:
        RuntimeClient(server["url"]).request("GET", "/v1/runs/unknown")
    assert error.value.code == "CONTROL_CONFLICT"
    assert TOKEN not in str(error.value) and "private.example" not in str(error.value)


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_never_forward_operator_credentials(server, status):
    server["status"] = status
    with pytest.raises(ClientError) as error:
        RuntimeClient(server["url"]).request("GET", "/v1/runs/unknown")
    assert error.value.code == "CLIENT_REDIRECT"
    assert len(server["requests"]) == 1


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        b"[]",
        b'{"status":"pass","result":NaN}',
        b'{"status":"pass","result":1e999}',
        b'{"status":"pass","result":1,"result":2}',
    ],
)
def test_malformed_protocol_never_becomes_success(server, body):
    server["override"] = body
    with pytest.raises(ClientError) as error:
        RuntimeClient(server["url"]).request("GET", "/v1/runs/unknown")
    assert error.value.code == "CLIENT_PROTOCOL"


def test_response_and_request_bounds(server, monkeypatch):
    monkeypatch.setattr(transport, "MAX_RESPONSE_BYTES", 16)
    with pytest.raises(ClientError) as error:
        RuntimeClient(server["url"]).request("GET", "/v1/runs/unknown")
    assert error.value.code == "CLIENT_PROTOCOL"
    with pytest.raises(ClientError) as error:
        RuntimeClient(server["url"]).request(
            "POST", "/v1/runs", {"too_much": "x" * transport.MAX_REQUEST_BYTES}
        )
    assert error.value.code == "CLIENT_INPUT_TOO_LARGE"
    assert len(server["requests"]) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example",
        "https://user:secret@example.com",
        "https://example.com/?secret=1",
        "https://example.com/#secret",
        "https://example.com/\nsecret",
        "file:///secret",
    ],
)
def test_remote_cleartext_and_embedded_secrets_rejected_without_reflection(url):
    with pytest.raises(ClientError) as error:
        RuntimeClient(url, token=TOKEN)
    assert error.value.code == "CLIENT_CONFIG"
    assert url not in str(error.value) and "secret" not in str(error.value)


@pytest.mark.parametrize("kind", ["step", "native"])
def test_download_verifies_identity_and_never_dumps_or_overwrites_bytes(
    server, tmp_path, capsys, kind
):
    run_id = str(uuid4())
    target = tmp_path / "delivery.bin"
    assert (
        cli(server, "download", run_id, "--task", "thicker", "--kind", kind, "--out", target) == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["result"]["revision_id"] == REVISION
    assert report["result"]["sha256"] == "sha256:" + hashlib.sha256(server["artifact"]).hexdigest()
    assert target.read_bytes() == server["artifact"]
    assert (
        cli(server, "download", run_id, "--task", "thicker", "--kind", kind, "--out", target) == 2
    )
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "OUTPUT_EXISTS"
    assert len(server["requests"]) == 1


def test_corrupt_download_has_no_visible_output(server, tmp_path, capsys):
    server["bad_hash"] = True
    target = tmp_path / "delivery.step"
    assert (
        cli(
            server, "download", str(uuid4()), "--task", "thicker", "--kind", "step", "--out", target
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "CLIENT_ARTIFACT_CORRUPT"
    assert not target.exists()


def test_download_size_bound_checked_before_exposing_bytes(server):
    with pytest.raises(ClientError) as error:
        RuntimeClient(server["url"]).download("/v1/runs/id/tasks/key/artifacts/step", max_bytes=4)
    assert error.value.code == "CLIENT_PROTOCOL"
