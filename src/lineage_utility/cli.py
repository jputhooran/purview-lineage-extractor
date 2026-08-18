"""CLI for config-driven lineage utility runs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .observability import configure_logging
from .orchestration import UtilityRunner, load_config
from .plugins import create_builtin_registry


def _common_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-c",
        "--config",
        required=True,
        type=Path,
        help="Version 1 utility YAML configuration",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    parser.add_argument(
        "--log-format",
        default="text",
        choices=("text", "json"),
    )
    parser.add_argument(
        "--no-external-plugins",
        action="store_true",
        help="Do not load installed entry-point plugins",
    )


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lineage-util",
        description=(
            "Run modular lineage extractors and publishers from one YAML "
            "configuration."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    plugins = commands.add_parser(
        "plugins", help="List available extractor and publisher plugins"
    )
    plugins.add_argument("--no-external-plugins", action="store_true")

    validate = commands.add_parser(
        "validate", help="Validate configuration and plugin construction"
    )
    _common_runtime_arguments(validate)

    plan = commands.add_parser(
        "plan",
        help="Extract models and create a manifest without remote writes",
    )
    _common_runtime_arguments(plan)
    plan.add_argument("--job", action="append", dest="jobs")

    run = commands.add_parser(
        "run", help="Extract and publish configured lineage jobs"
    )
    _common_runtime_arguments(run)
    run.add_argument("--job", action="append", dest="jobs")
    run.add_argument(
        "--force",
        action="store_true",
        help="Publish even when model and publisher configuration are unchanged",
    )
    run.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first target failure",
    )
    return parser


def _registry(no_external_plugins: bool):
    return create_builtin_registry(
        load_external=not no_external_plugins
    )


def main(argv: list[str] | None = None) -> int:
    args = create_parser().parse_args(argv)
    if args.command == "plugins":
        try:
            registry = _registry(args.no_external_plugins)
            print(
                json.dumps(
                    {
                        "extractors": registry.extractor_names,
                        "publishers": registry.publisher_names,
                    },
                    indent=2,
                )
            )
            return 0
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    try:
        configure_logging(
            level=args.log_level,
            output_format=args.log_format,
        )
        config = load_config(args.config)
        runner = UtilityRunner(
            config=config,
            registry=_registry(args.no_external_plugins),
        )
        if args.command == "validate":
            runner.validate_plugins()
            print(
                f"Configuration valid: {len(config.jobs)} job(s), "
                f"{len(config.publishers)} publisher(s)."
            )
            return 0
        summary = runner.run(
            selected_jobs=set(args.jobs) if args.jobs else None,
            dry_run=args.command == "plan",
            force=bool(getattr(args, "force", False)),
            fail_fast=bool(getattr(args, "fail_fast", False)),
        )
        print(
            f"Run {summary.run_id}: {summary.succeeded} succeeded, "
            f"{summary.failed} failed. Manifest: {summary.manifest_path}"
        )
        return 0 if summary.is_success else 1
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

