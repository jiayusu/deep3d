"""Submit, observe and control durable CAD work independently of a model provider."""

import argparse
import hashlib
import json
import os
import re
import tempfile
import urllib.parse
from pathlib import Path
from uuid import UUID, uuid4

from deep3d_cad.contracts import CadError
from deep3d_cad.jsonio import read_json

from .client import MAX_NATIVE_BYTES, MAX_REQUEST_BYTES, ClientError, RuntimeClient


def _uuid(value: str) -> str:
    try:
        identifier = UUID(value)
        if identifier.int == 0:
            raise ValueError
        return str(identifier)
    except (ValueError, TypeError, AttributeError):
        raise argparse.ArgumentTypeError("Expected a nonzero UUID") from None


def _nonnegative(value: str) -> int:
    try:
        result = int(value)
        if not 0 <= result <= 9_223_372_036_854_775_807:
            raise ValueError
        return result
    except ValueError:
        raise argparse.ArgumentTypeError("Expected a nonnegative i64") from None


def _input(path: Path) -> dict:
    try:
        return read_json(path, max_bytes=MAX_REQUEST_BYTES)
    except CadError:
        raise ClientError("CLIENT_INPUT", "Cannot read the strict JSON input") from None


def _head(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"revision_id", "generation"}:
        return False
    try:
        _uuid(value["revision_id"])
    except argparse.ArgumentTypeError:
        return False
    generation = value["generation"]
    return (
        isinstance(generation, str)
        and generation.isascii()
        and generation.isdigit()
        and (generation == "0" or not generation.startswith("0"))
        and len(generation) <= 20
        and int(generation) <= 18_446_744_073_709_551_615
    )


def _checkpoint(path: Path, value: dict) -> None:
    """Publish a fully fsynced new request without replacing an existing identity."""
    data = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
    _new_file(path, data, "Request checkpoint exists; retry with --request")


def _new_file(path: Path, data: bytes, exists_message: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".deep3d-submit-", suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Same-directory hard link gives create-new semantics on Windows and Unix;
        # the completed request is visible atomically before POST /v1/runs.
        os.link(temporary, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except FileExistsError:
        raise ClientError("OUTPUT_EXISTS", exists_message) from None
    except OSError:
        raise ClientError("CLIENT_IO", "Cannot persist the output file") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _submit(client: RuntimeClient, args: argparse.Namespace) -> dict:
    if args.request is not None:
        request = _input(args.request)
        if (
            set(request) != {"schema_version", "run_id", "project", "expected_head", "tasks"}
            or request["schema_version"] != "deep3d.submit-run/1"
            or not isinstance(request["project"], str)
            or not request["project"]
            or not _head(request["expected_head"])
            or not isinstance(request["tasks"], list)
        ):
            raise ClientError("CLIENT_INPUT", "Expected a saved deep3d.submit-run/1 request")
        try:
            _uuid(request["run_id"])
        except argparse.ArgumentTypeError:
            raise ClientError("CLIENT_INPUT", "Saved request has an invalid run UUID") from None
        return client.request("POST", "/v1/runs", request)
    if args.out.exists():
        raise ClientError("OUTPUT_EXISTS", "Request checkpoint exists; retry with --request")
    plan = _input(args.plan)
    if (
        set(plan) != {"schema_version", "tasks"}
        or plan["schema_version"] != "deep3d.task-plan/1"
        or not isinstance(plan["tasks"], list)
    ):
        raise ClientError("CLIENT_INPUT", "Expected a deep3d.task-plan/1 task list")
    try:
        with args.project.open("rb") as stream:
            native = stream.read(MAX_NATIVE_BYTES + 1)
    except OSError:
        raise ClientError("CLIENT_IO", "Cannot read the native project") from None
    if not native or len(native) > MAX_NATIVE_BYTES:
        raise ClientError("CLIENT_INPUT_TOO_LARGE", "Native package must be at most 16 MiB")
    imported = client.request("POST", "/v1/artifacts/native", native)
    source = imported.get("source") if isinstance(imported, dict) else None
    if (
        not isinstance(source, dict)
        or not isinstance(source.get("path"), str)
        or not source["path"]
        or not _head(source.get("head"))
        or source.get("sha256") != "sha256:" + hashlib.sha256(native).hexdigest()
    ):
        raise ClientError("CLIENT_PROTOCOL", "Imported project does not bind the uploaded bytes")
    request = {
        "schema_version": "deep3d.submit-run/1",
        "run_id": args.run_id or str(uuid4()),
        "project": source["path"],
        "expected_head": source["head"],
        "tasks": plan["tasks"],
    }
    _checkpoint(args.out, request)
    return client.request("POST", "/v1/runs", request)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8088")
    actions = parser.add_subparsers(dest="action", required=True)
    submit = actions.add_parser("submit")
    source = submit.add_mutually_exclusive_group(required=True)
    source.add_argument("--project", type=Path)
    source.add_argument("--request", type=Path, help="Retry an existing, identical submission")
    submit.add_argument("--plan", type=Path)
    submit.add_argument("--out", type=Path, help="New durable request checkpoint")
    submit.add_argument("--run-id", type=_uuid)
    status = actions.add_parser("status")
    status.add_argument("run_id", type=_uuid)
    events = actions.add_parser("events")
    events.add_argument("run_id", type=_uuid)
    events.add_argument("--after", type=_nonnegative, default=0)
    events.add_argument("--limit", type=int, choices=range(1, 1001), default=200, metavar="1..1000")
    control = actions.add_parser("control")
    control.add_argument("run_id", type=_uuid)
    control.add_argument(
        "--action", dest="control_action", required=True, choices=("pause", "resume", "cancel")
    )
    control.add_argument("--expected-epoch", type=_nonnegative, required=True)
    control.add_argument("--operation-id", type=_uuid, required=True)
    download = actions.add_parser("download")
    download.add_argument("run_id", type=_uuid)
    download.add_argument("--task", required=True)
    download.add_argument("--kind", required=True, choices=("step", "native"))
    download.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.action == "submit":
        if args.request is None and (args.plan is None or args.out is None):
            parser.error("submit --project requires --plan and --out")
        if args.request is not None and any(
            v is not None for v in (args.plan, args.out, args.run_id)
        ):
            parser.error("submit --request reuses saved identity; do not add --plan/--out/--run-id")
    try:
        client = RuntimeClient(args.api)
        if args.action == "submit":
            result = _submit(client, args)
        elif args.action == "status":
            result = client.request("GET", f"/v1/runs/{args.run_id}")
        elif args.action == "events":
            query = urllib.parse.urlencode({"after": args.after, "limit": args.limit})
            result = client.request("GET", f"/v1/runs/{args.run_id}/events?{query}")
        elif args.action == "download":
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.task):
                raise ClientError("CLIENT_INPUT", "Invalid task key")
            if args.out.exists():
                raise ClientError("OUTPUT_EXISTS", "Download output already exists")
            path = f"/v1/runs/{args.run_id}/tasks/{args.task}/artifacts/{args.kind}"
            limit = 256 * 1024 * 1024 if args.kind == "native" else 64 * 1024 * 1024
            data, identity = client.download(path, max_bytes=limit)
            _new_file(args.out, data, "Download output already exists")
            result = {
                "run_id": args.run_id,
                "task": args.task,
                "kind": args.kind,
                "output": str(args.out.resolve()),
                **identity,
            }
        else:
            result = client.request(
                "POST",
                f"/v1/runs/{args.run_id}/control",
                {
                    "operation_id": args.operation_id,
                    "expected_control_epoch": args.expected_epoch,
                    "action": args.control_action,
                },
            )
        print(json.dumps({"status": "pass", "result": result}, ensure_ascii=False))
        return 0
    except ClientError as exc:
        print(json.dumps({"status": "error", "error": {"code": exc.code, "message": str(exc)}}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
