"""Command-line installer for the private, per-user macOS application bundle."""

from __future__ import annotations

# ruff: noqa: UP045 -- supported Python 3.9 requires Optional rather than PEP 604 unions.
import argparse
import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Optional

from image23mf import __version__
from image23mf.macos.lifecycle import (
    APP_NAME,
    InstallError,
    MacOSPaths,
    install_release,
    load_workspace,
    uninstall_application,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"Install or remove {APP_NAME}")
    parser.add_argument("--home", type=Path, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    install = subparsers.add_parser("install", help="stage and activate a built release")
    install.add_argument("--wheel", type=Path, required=True)
    install.add_argument("--frontend", type=Path, required=True)
    install.add_argument("--release-version", default=__version__)
    install.add_argument("--workspace", type=Path)
    install.add_argument("--launch", action="store_true")

    subparsers.add_parser("status", help="show installed version and data locations")
    subparsers.add_parser("uninstall", help="remove the app while preserving user data")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    paths = MacOSPaths.for_home(args.home)
    try:
        if args.command == "install":
            result = install_release(
                wheel=args.wheel,
                frontend=args.frontend,
                version=args.release_version,
                paths=paths,
                workspace=args.workspace,
            )
            _print_json(
                {
                    "app": str(paths.app_bundle),
                    "backup": str(result.backup) if result.backup else None,
                    "logs": str(paths.logs),
                    "previous_version": result.previous_version,
                    "release": str(result.release),
                    "version": result.version,
                    "workspace": str(result.workspace) if result.workspace else None,
                }
            )
            if args.launch:
                subprocess.run(["/usr/bin/open", str(paths.app_bundle)], check=True)
            return 0
        if args.command == "uninstall":
            workspace = uninstall_application(paths)
            _print_json(
                {
                    "removed": str(paths.app_bundle),
                    "workspace_preserved": str(workspace) if workspace else "unknown",
                }
            )
            return 0
        _print_json(_status(paths))
        return 0
    except (InstallError, OSError, subprocess.CalledProcessError) as error:
        _print_json({"error": str(error), "ok": False})
        return 1


def _status(paths: MacOSPaths) -> dict:
    manifest = paths.current / "manifest.json"
    try:
        release = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        release = None
    try:
        workspace = load_workspace(paths)
    except InstallError:
        workspace = None
    runtime = paths.runtime / "server.json"
    try:
        running = json.loads(runtime.read_text(encoding="utf-8"))
        os.kill(int(running["pid"]), 0)
    except (OSError, ValueError, KeyError, TypeError):
        running = None
    return {
        "app": str(paths.app_bundle),
        "installed": release is not None,
        "logs": str(paths.logs),
        "release": release,
        "running": running,
        "workspace": str(workspace) if workspace else None,
    }


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
