"""Small deterministic CAD entry point; no LLM is required."""

import argparse
import json
import math
import sys
from pathlib import Path

from .contracts import CadError, FlangeSpec, PrecisionProfile
from .jsonio import read_json
from .runner import qualify, run_flange


def _timeout(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0 or result > 600:
        raise argparse.ArgumentTypeError("timeout must be finite and within (0, 600] seconds")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Build, reload BREP and independently reload STEP")
    build.add_argument("--spec", type=Path, required=True)
    build.add_argument("--goal", type=Path, help="Independent expected design, defaults to spec")
    build.add_argument("--precision", type=Path)
    build.add_argument("--out", type=Path, required=True)
    build.add_argument("--timeout", type=_timeout, default=120.0)
    qualification = commands.add_parser(
        "qualify", help="Real geometry positive/negative smoke cases"
    )
    qualification.add_argument("--out", type=Path, required=True)
    qualification.add_argument("--timeout", type=_timeout, default=120.0)
    args = parser.parse_args(argv)
    try:
        if args.command == "qualify":
            report = qualify(args.out, timeout=args.timeout)
        else:
            spec = FlangeSpec.from_dict(read_json(args.spec))
            goal = FlangeSpec.from_dict(read_json(args.goal)) if args.goal else spec
            precision = (
                PrecisionProfile.from_dict(read_json(args.precision))
                if args.precision
                else PrecisionProfile()
            )
            report = run_flange(
                spec, args.out, goal=goal, precision=precision, timeout=args.timeout
            )
        print(json.dumps({"status": report["status"], "output": str(args.out.resolve())}))
        return 0 if report["status"] == "pass" else 2
    except CadError as exc:
        print(
            json.dumps({"status": "error", "error": exc.to_dict()}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
