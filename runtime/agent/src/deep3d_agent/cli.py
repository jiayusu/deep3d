"""Command line entry point with concise machine-readable delivery results."""

import argparse
import json
import sys
from pathlib import Path

from deep3d_cad.contracts import CadError
from deep3d_cad.jsonio import read_json

from .provider import AnthropicProvider, ProviderError
from .runtime import AgentError, resume_agent, run_agent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("run", "resume"):
        action = actions.add_parser(name)
        action.add_argument("--out", type=Path, required=True, help="Durable local run directory")
        action.add_argument("--binary", type=Path, required=True, help="Trusted Rust deep3d CLI")
        action.add_argument("--python", type=Path, default=Path(sys.executable))
        action.add_argument("--model", required=True, help="Configured Anthropic-compatible model")
        if name == "run":
            action.add_argument("--project", type=Path, required=True)
            action.add_argument("--request", required=True)
            action.add_argument("--goal", type=Path, required=True)
            action.add_argument("--max-turns", type=int, default=8)
            action.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args()
    try:
        options = {
            "run_dir": args.out,
            "binary": args.binary,
            "python": args.python,
            "provider": AnthropicProvider(model=args.model),
        }
        if args.action == "run":
            result = run_agent(
                **options,
                project=args.project,
                request=args.request,
                goal=read_json(args.goal),
                max_turns=args.max_turns,
                max_attempts=args.max_attempts,
            )
        else:
            result = resume_agent(**options)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (AgentError, ProviderError, CadError) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": {"code": exc.code, "message": str(exc)},
                    "run": str(args.out.resolve()),
                },
                ensure_ascii=False,
            )
        )
    except OSError:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": {
                        "code": "RUN_IO",
                        "message": "Cannot access the project, binary or run files",
                    },
                }
            )
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
