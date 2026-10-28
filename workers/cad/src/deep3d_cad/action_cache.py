"""Immutable, local geometry-action cache; never a goal-completion authority.

Only trusted execution nodes may write this directory. Hashes detect corruption
and bind declared implementations; they are not remote worker attestation.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import stat
import tempfile
from pathlib import Path

from .contracts import CadError, PrecisionProfile
from .jsonio import read_json
from .runner import file_digest
from .worker import environment

_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_BREP = 64 * 1024 * 1024
_MAX_METADATA = 1024 * 1024
_IMPLEMENTATION_FILES = (
    "action_cache.py",
    "project_worker.py",
    "features.py",
    "sketch.py",
    "kernel.py",
    "contracts.py",
    "jsonio.py",
)


def _bytes(value: dict) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _normalize(value):
    # f64 numerical inputs and their equivalent integer JSON spelling share a key.
    # Ordered data (especially sketch constraints/seeds) retain their order.
    if type(value) in (int, float):
        number = float(value)
        if not math.isfinite(number):
            raise CadError("SCHEMA_INVALID", "Geometry action contains a non-finite value")
        return number if number != 0 else 0.0
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def geometry_identity() -> dict:
    """Bind actual adapter source and installed native/library bytes, not versions alone."""
    packages = {}
    for name in ("cadquery-ocp-novtk", "cadquery-ocp-proxy", "slvs"):
        distribution = importlib.metadata.distribution(name)
        files = {}
        for relative in distribution.files or ():
            label = relative.as_posix()
            lower = label.lower()
            if lower.endswith((".py", ".pyd", ".dll", ".dylib")) or ".so" in lower:
                path = Path(distribution.locate_file(relative))
                if not path.is_file():
                    raise CadError("ENGINE_IDENTITY_INVALID", "A native dependency file is missing")
                files[label] = file_digest(path)
        if not files:
            raise CadError("ENGINE_IDENTITY_INVALID", "Dependency has no identifiable source files")
        packages[name] = {"version": distribution.version, "files": files}
    return {
        "schema_version": "deep3d.geometry-engine/1",
        "runtime": environment(),
        "python_compiler": platform.python_compiler(),
        "libc": list(platform.libc_ver()),
        "implementation": {
            name: file_digest(Path(__file__).with_name(name)) for name in _IMPLEMENTATION_FILES
        },
        "packages": packages,
        "options": {
            "brep_format": 3,
            "triangulation": False,
            "boolean_parallel": False,
            "input_topology_copy": True,
        },
    }


def action_contract(
    feature: dict, input_artifacts: dict[str, str], precision: PrecisionProfile, engine: str
) -> tuple[str, dict]:
    if not _HASH.fullmatch(engine) or any(not _HASH.fullmatch(h) for h in input_artifacts.values()):
        raise CadError("CACHE_INPUT_INVALID", "Action inputs require exact content identities")
    action = {
        "schema_version": "deep3d.geometry-action/1",
        "op": feature["op"],
        "parameters": _normalize(feature["parameters"]),
        "data": _normalize(feature.get("data", {})),
        "input_artifacts": input_artifacts,
        # Other PrecisionProfile fields judge evidence; they do not construct geometry.
        "geometry_precision": {"working_tolerance_mm": precision.working_tolerance_mm},
        "engine_sha256": engine,
        "output_type": "profile" if feature["op"].startswith("profile.") else "solid",
    }
    return _digest(_bytes(action)), action


def _read(path: Path, maximum: int) -> bytes:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > maximum:
            raise CadError("CACHE_CORRUPT", "Cache object is not a bounded regular file")
        with path.open("rb") as stream:
            data = stream.read(maximum + 1)
        if len(data) != info.st_size or len(data) > maximum:
            raise CadError("CACHE_CORRUPT", "Cache object changed during reading")
        return data
    except OSError as exc:
        raise CadError("CACHE_CORRUPT", "Cache reference is missing or unreadable") from exc


def _sync_parent(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _publish(path: Path, data: bytes, *, conflict: str = "CACHE_CORRUPT") -> None:
    """Publish a fsynced complete file with create-new, never replace, semantics."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".pending-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
            _sync_parent(path)
        except FileExistsError:
            if _read(path, max(len(data), _MAX_METADATA)) != data:
                raise CadError(conflict, "An immutable cache address has different bytes") from None
    except OSError as exc:
        raise CadError("CACHE_IO", "Cannot persist immutable geometry cache") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _checked_shape(path: Path, output_type: str):
    from OCP.BRep import BRep_Tool
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE, TopAbs_WIRE
    from OCP.TopoDS import TopoDS

    from .features import _map, _solid
    from .kernel import read_brep

    try:
        shape = read_brep(path)
        if output_type == "solid":
            return _solid(shape, "cache.solid")
        if shape.ShapeType() != TopAbs_FACE:
            raise CadError("CACHE_CORRUPT", "Cached profile is not one face")
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(shape, props)
        area = props.Mass()
        wires = _map(shape, TopAbs_WIRE)
        if (
            not math.isfinite(area)
            or area <= 1e-12
            or wires.Extent() == 0
            or any(
                not BRep_Tool.IsClosed_s(TopoDS.Wire(wires.FindKey(i)))
                for i in range(1, wires.Extent() + 1)
            )
        ):
            raise CadError("CACHE_CORRUPT", "Cached profile is open or has negligible area")
        return shape
    except CadError as exc:
        if exc.code == "CACHE_CORRUPT":
            raise
        raise CadError("CACHE_CORRUPT", "Cached BREP fails native geometry checks") from exc


class ActionCache:
    def __init__(self, root: Path, identity: dict):
        self.root = root.resolve()
        self.identity = identity
        identity_bytes = _bytes(identity)
        self.engine_sha256 = _digest(identity_bytes)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            for name in ("entries", "objects", "engines"):
                folder = self.root / name
                if folder.is_symlink():
                    raise CadError("CACHE_PATH_INVALID", "Cache folders must not be symbolic links")
                folder.mkdir(exist_ok=True)
        except OSError as exc:
            raise CadError("CACHE_IO", "Cannot create geometry cache") from exc
        _publish(self.root / "engines" / f"{self.engine_sha256[7:]}.json", identity_bytes)

    def restore(self, key: str, action: dict, destination: Path):
        if not _HASH.fullmatch(key) or key != _digest(_bytes(action)):
            raise CadError("CACHE_INPUT_INVALID", "Action contract does not match its identity")
        entry = self.root / "entries" / f"{key[7:]}.json"
        if not entry.exists() and not entry.is_symlink():
            return None
        try:
            _read(entry, _MAX_METADATA)
            metadata = read_json(entry)
            if (
                set(metadata)
                != {"schema_version", "action_key", "action", "artifact_sha256", "size_bytes"}
                or metadata["schema_version"] != "deep3d.action-cache-entry/1"
                or metadata["action_key"] != key
                or metadata["action"] != action
                or not isinstance(metadata["artifact_sha256"], str)
                or not _HASH.fullmatch(metadata["artifact_sha256"])
                or type(metadata["size_bytes"]) is not int
                or not 0 < metadata["size_bytes"] <= _MAX_BREP
                or action["engine_sha256"] != self.engine_sha256
            ):
                raise CadError("CACHE_CORRUPT", "Cache entry is not bound to the requested action")
            engine = self.root / "engines" / f"{self.engine_sha256[7:]}.json"
            if _read(engine, _MAX_METADATA) != _bytes(self.identity):
                raise CadError("CACHE_CORRUPT", "Cache engine identity is not intact")
            artifact = self.root / "objects" / f"{metadata['artifact_sha256'][7:]}.brep"
            data = _read(artifact, _MAX_BREP)
            if len(data) != metadata["size_bytes"] or _digest(data) != metadata["artifact_sha256"]:
                raise CadError("CACHE_CORRUPT", "Cached geometry hash or length differs")
            # Parse a private byte-for-byte copy, never the shared mutable path.
            with destination.open("xb") as stream:
                stream.write(data)
            shape = _checked_shape(destination, action["output_type"])
            return shape, metadata["artifact_sha256"]
        except CadError as exc:
            if exc.code.startswith("CACHE_"):
                raise
            raise CadError("CACHE_CORRUPT", "Cache metadata cannot be decoded") from exc
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise CadError("CACHE_CORRUPT", "Cannot restore cached geometry") from exc

    def publish(self, key: str, action: dict, source: Path) -> None:
        if (
            not _HASH.fullmatch(key)
            or key != _digest(_bytes(action))
            or action["engine_sha256"] != self.engine_sha256
        ):
            raise CadError("CACHE_INPUT_INVALID", "Cannot publish a foreign action")
        data = _read(source, _MAX_BREP)
        _checked_shape(source, action["output_type"])
        artifact = _digest(data)
        metadata = {
            "schema_version": "deep3d.action-cache-entry/1",
            "action_key": key,
            "action": action,
            "artifact_sha256": artifact,
            "size_bytes": len(data),
        }
        _publish(self.root / "objects" / f"{artifact[7:]}.brep", data)
        _publish(
            self.root / "entries" / f"{key[7:]}.json", _bytes(metadata), conflict="CACHE_CONFLICT"
        )
