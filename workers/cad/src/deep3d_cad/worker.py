"""One trusted CAD action per process. This module is not a script execution API."""

import argparse
import importlib.metadata
import os
import platform
import sys
from pathlib import Path

from .contracts import CadError, FlangeSpec, PrecisionProfile
from .jsonio import read_json, write_json
from .runner import file_digest


def environment() -> dict:
    import OCP

    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "system": platform.system(),
        "machine": platform.machine(),
        "ocp_binding_version": OCP.__version__,
        "cadquery_ocp_novtk": importlib.metadata.version("cadquery-ocp-novtk"),
        "deep3d_cad": importlib.metadata.version("deep3d-cad"),
    }


def execute(args: argparse.Namespace) -> dict:
    from .kernel import build_flange, export_step, import_step, read_brep, write_brep
    from .verifier import verify_flange

    spec = FlangeSpec.from_dict(read_json(args.spec))
    precision = PrecisionProfile.from_dict(read_json(args.precision))
    if args.action == "build":
        result = build_flange(spec, working_tolerance_mm=precision.working_tolerance_mm)
        write_brep(result.shape, args.directory / "flange.brep")
        exchange = export_step(
            result.shape,
            args.directory / "flange.step",
            working_tolerance_mm=precision.working_tolerance_mm,
        )
        return {
            "schema_version": "deep3d.build/1",
            "status": "pass",
            "environment": environment(),
            "worker_pid": os.getpid(),
            "operations": result.operations,
            "exchange": exchange,
        }
    input_digest = file_digest(args.shape)
    if args.action == "verify-brep":
        shape = read_brep(args.shape)
        stage = "native"
    else:
        shape = import_step(args.shape)
        stage = "step"
    report = verify_flange(shape, spec, precision, stage=stage)
    if file_digest(args.shape) != input_digest:
        raise CadError("ARTIFACT_CHANGED", "Geometry artifact changed during verification")
    report["input_artifact_sha256"] = input_digest
    report["validator_id"] = "deep3d.analytic-flange@1"
    report["validator_source_sha256"] = file_digest(
        Path(sys.modules[verify_flange.__module__].__file__)
    )
    report["environment"] = environment()
    report["worker_pid"] = os.getpid()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["build", "verify-brep", "verify-step"])
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--precision", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--shape", type=Path)
    args = parser.parse_args()
    if args.action == "build" and args.directory is None:
        parser.error("build requires --directory")
    if args.action != "build" and args.shape is None:
        parser.error("verification requires --shape")
    try:
        report = execute(args)
    except CadError as exc:
        report = {"status": "error", "error": exc.to_dict()}
    except Exception as exc:
        # Native failures remain visible and never become successful empty geometry.
        report = {
            "status": "error",
            "error": {"code": "WORKER_ERROR", "message": str(exc), "type": type(exc).__name__},
        }
    write_json(args.report, report)
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    sys.exit(main())
