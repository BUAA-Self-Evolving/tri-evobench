"""Command-line entry point for running one materialized L2 Stream."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from l2_runtime.api import OpenAIChatModel
from l2_runtime.runner import StreamRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stream_dir", type=Path)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(__file__).resolve().parents[4] / "runs",
    )
    parser.add_argument("--api-base", default=os.environ.get("L2_API_BASE"))
    parser.add_argument("--model", default=os.environ.get("L2_MODEL", "gpt-5.6-terra"))
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument(
        "--allow-unsafe-code-execution",
        action="store_true",
        help="Required: execute model-generated Python locally without a security sandbox.",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="Authenticate, print provider model IDs, and exit without running a Stream.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.api_base:
        raise SystemExit("L2_API_BASE or --api-base is required")
    model = OpenAIChatModel.from_environment(
        base_url=args.api_base,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
    )
    if args.list_models:
        print("\n".join(model.list_model_ids()))
        return 0
    if not args.allow_unsafe_code_execution:
        raise SystemExit(
            "refusing to execute generated code without --allow-unsafe-code-execution"
        )
    report = StreamRunner(
        stream_dir=args.stream_dir,
        model=model,
        output_root=args.output_root,
        allow_unsafe_execution=True,
    ).run()
    print(
        json.dumps(
            {
                "success": report["success"],
                "stream_id": report["stream_id"],
                "run_dir": report["run_dir"],
                "episodes": report["episodes"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
