"""Bounded, strict JSON at the worker boundary."""

import json
import math
import os
from pathlib import Path
from uuid import uuid4

from .contracts import CadError

MAX_JSON_BYTES = 1_048_576
MAX_REPORT_BYTES = 8_388_608


def _pairs(items: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            raise CadError("SCHEMA_INVALID", "Duplicate JSON member", {"member": key})
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise CadError("SCHEMA_INVALID", "Non-finite JSON number", {"value": value})


def _float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise CadError("SCHEMA_INVALID", "JSON number overflows finite f64")
    return result


def _validate_depth_and_strings(value: object) -> None:
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 32:
            raise CadError("SCHEMA_INVALID", "JSON nesting exceeds 32 levels")
        if isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeError as exc:
                raise CadError(
                    "SCHEMA_INVALID", "JSON contains unpaired Unicode surrogates"
                ) from exc
        elif isinstance(item, dict):
            pending.extend((key, depth + 1) for key in item)
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)


def read_json(path: Path, *, max_bytes: int = MAX_JSON_BYTES) -> dict:
    try:
        with path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise CadError(
                "INPUT_TOO_LARGE",
                "JSON exceeds the configured size limit",
                {"max_bytes": max_bytes},
            )
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
            parse_float=_float,
        )
    except RecursionError as exc:
        raise CadError("SCHEMA_INVALID", "JSON nesting exceeds parser limit") from exc
    except (OSError, UnicodeError, ValueError) as exc:
        raise CadError(
            "INPUT_UNREADABLE", "Cannot read strict UTF-8 JSON", {"path": str(path)}
        ) from exc
    if not isinstance(value, dict):
        raise CadError("SCHEMA_INVALID", "Expected a JSON object")
    _validate_depth_and_strings(value)
    return value


def write_json(path: Path, value: dict) -> None:
    """Write in the caller-owned directory; an interrupted write never looks complete."""
    data = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
