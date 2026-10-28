"""Real coordinator + PostgreSQL + CAD worker processes for recovery scenarios.

Faults are injected from outside the system: POSIX signals on real worker
process groups, a coordinator restart, and an HTTP proxy that can drop or delay
worker responses after the coordinator has already decided. Nothing here
writes the authority database or calls coordinator internals directly; the
database is only read to prove uniqueness and epochs.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "target" / "debug"
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class ApiError(Exception):
    def __init__(self, status: int, body: dict):
        super().__init__(f"{status}: {json.dumps(body)[:500]}")
        self.status, self.body = status, body

    @property
    def code(self) -> str:
        return (self.body.get("error") or {}).get("code", "")


def request(url: str, token: str, method="GET", payload=None, raw=False, timeout=60):
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if isinstance(payload, bytes):
        data, headers["Content-Type"] = payload, "application/octet-stream"
    elif payload is not None:
        data, headers["Content-Type"] = json.dumps(payload).encode(), "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
            if raw:
                return body, dict(response.headers)
            return json.loads(body)["result"]
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            raise ApiError(exc.code, json.loads(body)) from None
        except json.JSONDecodeError:
            raise ApiError(exc.code, {"raw": body.decode(errors="replace")}) from None


class FaultProxy:
    """Forwards worker traffic; selected responses are dropped after delivery.

    `drop_complete` counts completion responses to discard: the request still
    reaches the coordinator and commits, but the worker sees a reset.
    `hold_complete_seconds` delays forwarding later completions.
    """

    def __init__(self, upstream: str):
        self.upstream = upstream
        self.drop_complete = 0
        self.hold_complete_seconds = 0.0
        self.log: list[dict] = []
        self.lock = threading.Lock()
        # Closed gate: completions wait at the proxy (candidate bytes exist,
        # acceptance has not started). `discard_held` answers them with 503
        # instead of forwarding when the gate opens.
        self.gate = threading.Event()
        self.gate.set()
        self.discard_held = False
        self.held: list[float] = []
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _forward(self, method):
                arrived = time.monotonic()
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                complete = self.path.endswith("/worker/complete")
                drop = False
                with proxy.lock:
                    if complete and proxy.drop_complete > 0:
                        proxy.drop_complete -= 1
                        drop = True
                if complete and not drop and not proxy.gate.is_set():
                    proxy.held.append(arrived)
                    proxy.gate.wait()
                    if proxy.discard_held:
                        proxy.log.append(
                            {
                                "arrived": arrived,
                                "path": self.path,
                                "status": 503,
                                "discarded": True,
                                "request_sha256": sha256(body or b""),
                                "monotonic": time.monotonic(),
                            }
                        )
                        self.send_response(503)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                if complete and not drop and proxy.hold_complete_seconds:
                    time.sleep(proxy.hold_complete_seconds)
                headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host",)}
                req = urllib.request.Request(
                    proxy.upstream + self.path, data=body, method=method, headers=headers
                )
                try:
                    with urllib.request.urlopen(req, timeout=120) as response:
                        status, payload = response.status, response.read()
                        reply_headers = dict(response.headers)
                except urllib.error.HTTPError as exc:
                    status, payload, reply_headers = exc.code, exc.read(), dict(exc.headers)
                entry = {
                    "arrived": arrived,
                    "path": self.path,
                    "status": status,
                    "request_sha256": sha256(body or b""),
                    "response": json.loads(payload) if payload.startswith(b"{") else None,
                    "monotonic": time.monotonic(),
                }
                if drop:
                    entry["dropped_after_commit"] = True
                    proxy.log.append(entry)
                    # Close without any response: the caller cannot know the outcome.
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
                proxy.log.append(entry)
                self.send_response(status)
                for key, value in reply_headers.items():
                    if key.lower() in ("content-type",):
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self._forward("GET")

            def do_POST(self):
                self._forward("POST")

        self.port = free_port()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()


class Stack:
    def __init__(self, work: Path, admin_url: str, lease_seconds: int = 4):
        self.work = work
        self.work.mkdir(parents=True)
        self.database = "g5_" + uuid4().hex[:12]
        subprocess.run(["psql", admin_url, "-qc", f"CREATE DATABASE {self.database}"], check=True)
        base = admin_url.rsplit("/", 1)[0]
        self.db_url = f"{base}/{self.database}"
        self.operator = "op-" + secrets.token_hex(16)
        self.worker_token = "wk-" + secrets.token_hex(16)
        self.lease_seconds = lease_seconds
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.root = work / "service-root"
        self.service = None
        self.service_starts: list[dict] = []
        self.workers: dict[str, dict] = {}
        self.start_service()

    # Processes ---------------------------------------------------------------
    def start_service(self):
        env = {
            **os.environ,
            "DEEP3D_DATABASE_URL": self.db_url,
            "DEEP3D_OPERATOR_TOKEN": self.operator,
            "DEEP3D_WORKER_TOKEN": self.worker_token,
            "DEEP3D_VERIFIER_PYTHON": str(VENV_PYTHON),
        }
        log = (self.work / f"service-{len(self.service_starts)}.log").open("wb")
        self.service = subprocess.Popen(
            [
                str(BIN / "deep3d-runtime"),
                "serve",
                "--bind",
                f"127.0.0.1:{self.port}",
                "--root",
                str(self.root),
                "--lease-seconds",
                str(self.lease_seconds),
            ],
            env=env,
            stdout=log,
            stderr=log,
            cwd=ROOT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(self.url + "/health", timeout=1).read()
                self.service_starts.append({"pid": self.service.pid, "at": time.time()})
                return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("coordinator did not start")

    def kill_service(self):
        os.killpg(self.service.pid, signal.SIGKILL)
        self.service.wait()
        return self.service.pid

    def start_worker(self, name: str, run_id: str, api: str | None = None) -> dict:
        # Like the Compose deployment: workers read the coordinator's object
        # store and share its attempt workspace; logs stay per worker.
        logs = self.work / f"worker-{name}"
        logs.mkdir(exist_ok=True)
        env = {**os.environ, "DEEP3D_WORKER_TOKEN": self.worker_token}
        log = (logs / f"worker-{len([w for w in self.workers if w.startswith(name)])}.log").open(
            "ab"
        )
        process = subprocess.Popen(
            [
                str(VENV_PYTHON),
                "-m",
                "deep3d_worker",
                "--api",
                api or self.url,
                "--root",
                str(self.root),
                "--binary",
                str(BIN / "deep3d"),
                "--python",
                str(VENV_PYTHON),
                "--concurrency",
                "1",
                "--worker-id",
                name,
                "--run-id",
                run_id,
                "--poll-seconds",
                "0.2",
                "--shutdown-grace",
                "1",
            ],
            env=env,
            stdout=log,
            stderr=log,
            cwd=ROOT,
            start_new_session=True,
        )
        record = {"name": name, "pid": process.pid, "process": process, "logs": logs}
        self.workers[f"{name}#{process.pid}"] = record
        return record

    def descendants(self, pid: int) -> list[dict]:
        out = subprocess.run(["ps", "-eo", "pid,ppid,pgid,args"], capture_output=True, text=True)
        rows = []
        for line in out.stdout.splitlines()[1:]:
            parts = line.split(None, 3)
            if len(parts) == 4:
                rows.append(
                    {
                        "pid": int(parts[0]),
                        "ppid": int(parts[1]),
                        "pgid": int(parts[2]),
                        "args": parts[3],
                    }
                )
        children, frontier = [], {pid}
        while frontier:
            found = [r for r in rows if r["ppid"] in frontier]
            children += found
            frontier = {r["pid"] for r in found}
        return children

    def wait_held(self, proxy: "FaultProxy", timeout=180) -> float:
        """Block until a completion is waiting at the closed proxy gate."""
        return self.wait(lambda: proxy.held[0] if proxy.held else None, timeout, "held completion")

    def wait_for_cad(self, worker: dict, timeout=120) -> list[dict]:
        """Block until the worker has a live native CAD evaluation process."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            active = [
                p
                for p in self.descendants(worker["pid"])
                if "project_worker" in p["args"] or " evaluate " in p["args"]
            ]
            if any("project_worker" in p["args"] for p in active):
                return active
            time.sleep(0.05)
        raise RuntimeError("worker never started native CAD evaluation")

    def signal_worker(self, worker: dict, sig) -> list[int]:
        group = [worker["pid"], *(p["pid"] for p in self.descendants(worker["pid"]))]
        os.killpg(worker["pid"], sig)
        return group

    def stop_all(self):
        for worker in self.workers.values():
            if worker["process"].poll() is None:
                try:
                    os.killpg(worker["pid"], signal.SIGCONT)
                    os.killpg(worker["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
                worker["process"].wait()
        if self.service and self.service.poll() is None:
            os.killpg(self.service.pid, signal.SIGTERM)
            try:
                self.service.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(self.service.pid, signal.SIGKILL)

    # Operator API ------------------------------------------------------------
    def api(self, method, path, payload=None, raw=False):
        return request(self.url + path, self.operator, method, payload, raw=raw)

    def upload(self, package: Path) -> dict:
        return self.api("POST", "/v1/artifacts/native", package.read_bytes())

    def register(self, source: dict) -> dict:
        return self.api(
            "POST",
            "/v1/projects",
            {
                "operation_id": str(uuid4()),
                "project": source["path"],
                "expected_head": source["head"],
            },
        )

    def set_goal(self, project_id, acceptance, request_text, expected_goal, generation, precision):
        return self.api(
            "POST",
            f"/v1/projects/{project_id}/goals",
            {
                "operation_id": str(uuid4()),
                "expected_goal": expected_goal,
                "expected_generation": str(generation),
                "definition": {
                    "schema_version": "deep3d.goal/1",
                    "original_request": request_text,
                    "source_refs": ["g5-harness:registered-recovery-scenario"],
                    "acceptance": acceptance,
                    "precision": precision,
                    "unresolved": [],
                },
            },
        )

    def submit(self, source, tasks, binding=None, run_id=None) -> dict:
        body = {
            "schema_version": "deep3d.submit-run/1",
            "run_id": run_id or str(uuid4()),
            "project": source["path"],
            "expected_head": source["head"],
            "tasks": tasks,
        }
        if binding:
            body["authority"] = binding
        return self.api("POST", "/v1/runs", body)

    def project(self, project_id) -> dict:
        return self.api("GET", f"/v1/projects/{project_id}")

    def run(self, run_id) -> dict:
        return self.api("GET", f"/v1/runs/{run_id}")

    def public_digest(self, project_id, kind) -> str:
        body, _ = self.api("GET", f"/v1/projects/{project_id}/public/artifacts/{kind}", raw=True)
        return sha256(body)

    def wait_run(self, run_id, states=("completed", "failed", "cancelled"), timeout=300) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            view = self.run(run_id)
            if view["state"] in states:
                return view
            time.sleep(0.2)
        raise RuntimeError(f"run {run_id} did not reach {states}: {view['state']}")

    def wait(self, predicate, timeout=120, label="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.2)
        raise RuntimeError(f"timed out waiting for {label}")

    def sql(self, query: str) -> list[list[str]]:
        out = subprocess.run(
            ["psql", self.db_url, "-AtF", "\t", "-c", query],
            capture_output=True,
            text=True,
            check=True,
        )
        return [line.split("\t") for line in out.stdout.splitlines() if line]


def main():  # pragma: no cover - manual smoke
    print("import only", file=sys.stderr)


if __name__ == "__main__":
    main()
