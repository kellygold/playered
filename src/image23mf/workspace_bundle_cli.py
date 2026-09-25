"""Local command-line entry point for workspace backup and disaster recovery."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from image23mf.workspace_bundles import (
    WorkspaceBundleError,
    WorkspaceBundleService,
    WorkspaceConflictError,
    WorkspaceSwapRecoveryError,
    reveal_workspace_bundle_in_finder,
    reveal_workspace_in_finder,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Back up or restore an Image23MF workspace")
    commands = parser.add_subparsers(dest="command", required=True)

    backup = commands.add_parser("backup", help="create a checksummed whole-workspace archive")
    backup.add_argument("--workspace", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)

    preflight = commands.add_parser("preflight", help="verify an archive and inspect its target")
    preflight.add_argument("--bundle", type=Path, required=True)
    preflight.add_argument("--target", type=Path, required=True)

    restore = commands.add_parser("restore", help="stage, validate, and atomically restore")
    restore.add_argument("--bundle", type=Path, required=True)
    restore.add_argument("--target", type=Path, required=True)
    restore.add_argument(
        "--choice",
        choices=("empty_only", "replace"),
        default="empty_only",
        help="replace is explicit and creates an automatic rollback archive first",
    )

    reveal = commands.add_parser("reveal", help="verify and reveal a backup in Finder")
    reveal.add_argument("--bundle", type=Path, required=True)
    reveal_workspace = commands.add_parser(
        "reveal-workspace", help="validate and reveal a workspace in Finder"
    )
    reveal_workspace.add_argument("--workspace", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "backup":
            result = WorkspaceBundleService(args.workspace).export(args.output)
            _print(
                {
                    "ok": True,
                    "path": str(result.path),
                    "sha256": result.sha256,
                    "byte_size": result.byte_size,
                    "manifest": result.manifest.model_dump(mode="json"),
                }
            )
            return 0
        verifier = WorkspaceBundleService(args.target if hasattr(args, "target") else Path.cwd())
        if args.command == "preflight":
            _print(
                {
                    "ok": True,
                    "preflight": verifier.preflight(args.bundle, args.target).model_dump(
                        mode="json"
                    ),
                }
            )
            return 0
        if args.command == "restore":
            report = verifier.restore(args.bundle, args.target, choice=args.choice)
            _print({"ok": True, "report": report.model_dump(mode="json")})
            return 0
        if args.command == "reveal-workspace":
            result = reveal_workspace_in_finder(args.workspace)
            _print({"ok": True, "result": result.__dict__})
            return 0
        result = reveal_workspace_bundle_in_finder(args.bundle, verifier=verifier)
        _print({"ok": True, "result": result.__dict__})
        return 0
    except WorkspaceConflictError as error:
        _print(
            {
                "ok": False,
                "error": type(error).__name__,
                "message": str(error),
                "preflight": error.preflight.model_dump(mode="json"),
            }
        )
        return 2
    except WorkspaceSwapRecoveryError as error:
        _print(
            {
                "ok": False,
                "error": type(error).__name__,
                "message": str(error),
                "recovery": {
                    "previous_workspace": str(error.previous_workspace),
                    "staged_workspace": str(error.staged_workspace),
                    "rollback_bundle": str(error.rollback_bundle)
                    if error.rollback_bundle
                    else None,
                    "target": str(error.target),
                },
            }
        )
        return 3
    except WorkspaceBundleError as error:
        _print({"ok": False, "error": type(error).__name__, "message": str(error)})
        return 1


def _print(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
