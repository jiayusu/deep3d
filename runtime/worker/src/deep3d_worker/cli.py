"""Persistent multi-slot CAD worker. All scheduling authority lives in the runtime API."""

import argparse
import json
import random
import re
import signal
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

from .client import RuntimeClient, WorkerError
from .runner import TaskRunner

_LOG_LOCK = threading.Lock()


def emit(event: str, **details: object) -> None:
    with _LOG_LOCK:
        print(json.dumps({"event": event, **details}), flush=True)


class WorkerPool:
    def __init__(
        self,
        runner: TaskRunner,
        *,
        concurrency: int = 2,
        worker_id: str | None = None,
        poll_seconds: float = 1,
        shutdown_grace: float = 30,
        run_id: str | None = None,
    ):
        prefix = worker_id or "worker-" + uuid4().hex
        if (
            not 1 <= concurrency <= 32
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", prefix)
            or not 0.05 <= poll_seconds <= 60
            or not 0 <= shutdown_grace <= 600
        ):
            raise WorkerError("WORKER_CONFIG")
        if run_id is not None:
            try:
                if str(UUID(run_id)) != run_id:
                    raise ValueError
            except ValueError:
                raise WorkerError("WORKER_CONFIG") from None
        self.runner, self.concurrency, self.prefix = runner, concurrency, prefix
        self.poll_seconds, self.shutdown_grace, self.run_id = poll_seconds, shutdown_grace, run_id
        self.stop, self.force = threading.Event(), threading.Event()
        self.fatal = threading.Event()

    def _slot(self, slot: int) -> None:
        worker = f"{self.prefix}-{slot}"
        request = None
        backoff = 0.25
        last_error = None
        while not self.stop.is_set():
            if request is None:
                request = {"claim_id": str(uuid4()), "worker_id": worker, "run_id": self.run_id}
            try:
                lease = self.runner.client.post("claim", request)
                request = None
                last_error = None
                backoff = 0.25
                if lease is None:
                    self.stop.wait(self.poll_seconds * random.uniform(0.8, 1.2))
                    continue
                result = self.runner.execute(lease, self.force)
                emit(
                    "task_completed",
                    worker_id=worker,
                    run_id=lease["token"]["run_id"],
                    task_key=lease["token"]["task_key"],
                    output=result,
                )
            except WorkerError as exc:
                if exc.code != last_error:
                    emit("worker_blocked", worker_id=worker, code=exc.code)
                    last_error = exc.code
                if exc.code in {"WORKER_AUTH", "WORKER_CONFIG", "RUNTIME_REDIRECT"}:
                    self.fatal.set()
                    self.stop.set()
                    return
                if not exc.retryable:
                    request = None
                self.stop.wait(backoff)
                backoff = min(10, backoff * 2)
            except Exception:
                emit("worker_blocked", worker_id=worker, code="WORKER_INTERNAL")
                request = None
                self.stop.wait(1)

    def _outbox(self) -> None:
        while not self.stop.is_set():
            try:
                count = self.runner.replay_pending(self.stop)
                if count:
                    emit("completion_receipts_recovered", count=count)
            except WorkerError as exc:
                emit("worker_blocked", code=exc.code)
                if exc.code == "WORKER_AUTH":
                    self.fatal.set()
                    self.stop.set()
            self.stop.wait(5)

    def serve(self) -> int:
        slots = [
            threading.Thread(target=self._slot, args=(number,), daemon=True)
            for number in range(self.concurrency)
        ]
        outbox = threading.Thread(target=self._outbox, daemon=True)
        emit("worker_started", worker_id=self.prefix, concurrency=self.concurrency)
        for thread in [*slots, outbox]:
            thread.start()
        try:
            while not self.stop.wait(0.2):
                pass
        except KeyboardInterrupt:
            self.stop.set()
        deadline = time.monotonic() + self.shutdown_grace
        while any(thread.is_alive() for thread in slots) and time.monotonic() < deadline:
            for thread in slots:
                thread.join(timeout=0.1)
        self.force.set()
        for thread in slots:
            thread.join(timeout=12)
        outbox.join(timeout=12)
        emit("worker_stopped")
        return 2 if self.fatal.is_set() else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8088")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--worker-id")
    parser.add_argument("--run-id")
    parser.add_argument("--poll-seconds", type=float, default=1)
    parser.add_argument("--shutdown-grace", type=float, default=30)
    args = parser.parse_args()
    try:
        runner = TaskRunner(RuntimeClient(args.api), args.root, args.binary, args.python)
        pool = WorkerPool(
            runner,
            concurrency=args.concurrency,
            worker_id=args.worker_id,
            poll_seconds=args.poll_seconds,
            shutdown_grace=args.shutdown_grace,
            run_id=args.run_id,
        )
        for name in (signal.SIGINT, signal.SIGTERM):
            signal.signal(name, lambda *_: pool.stop.set())
        return pool.serve()
    except WorkerError as exc:
        emit("worker_error", code=exc.code)
    except OSError:
        emit("worker_error", code="WORKER_IO")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
