"""Loopback-only desktop launcher for the local Image23MF web application."""

from __future__ import annotations

# ruff: noqa: UP045 -- supported Python 3.9 requires Optional rather than PEP 604 unions.
import argparse
import fcntl
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional

from image23mf import __version__
from image23mf.macos.lifecycle import DATABASE_FILENAME, InstallError, MacOSPaths

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8323
DEFAULT_WORKSPACE_NAME = "Image23MF Studio"
LOGGER = logging.getLogger("image23mf.macos")


def resolve_workspace(
    paths: MacOSPaths,
    *,
    explicit: Optional[Path] = None,
    environment: Optional[Mapping[str, str]] = None,
    chooser: Optional[Callable[[], Optional[Path]]] = None,
) -> Path:
    """Resolve and persist a workspace without ever placing it inside a release."""

    from image23mf.macos.lifecycle import load_workspace, save_workspace

    env = os.environ if environment is None else environment
    candidate = explicit
    if candidate is None and env.get("IMAGE23MF_WORKSPACE"):
        candidate = Path(env["IMAGE23MF_WORKSPACE"])
    if candidate is None:
        candidate = load_workspace(paths)
    if candidate is None and chooser is not None:
        candidate = chooser()
    if candidate is None:
        candidate = paths.home / "Documents" / DEFAULT_WORKSPACE_NAME
    return save_workspace(paths, candidate)


def choose_workspace() -> Optional[Path]:
    """Ask Finder for a workspace. Cancellation safely falls back to Documents."""

    if sys.platform != "darwin":
        return None
    script = (
        'POSIX path of (choose folder with prompt "Choose where Image23MF Studio stores '
        'projects, source images, exports, and its database")'
    )
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    value = result.stdout.strip()
    return Path(value) if value else None


def migrate_workspace(workspace: Path) -> None:
    """Open the database once so the release's forward migrations complete."""

    from image23mf.storage import open_database

    workspace.mkdir(parents=True, exist_ok=True)
    connection = open_database(workspace / DATABASE_FILENAME)
    connection.close()


def create_local_app(workspace: Path, web_root: Path):
    """Build the API and serve the immutable production frontend behind it."""

    from fastapi.staticfiles import StaticFiles

    from image23mf.api.app import create_app
    from image23mf.settings import Settings

    if not (web_root / "index.html").is_file():
        raise InstallError(f"installed frontend is incomplete: {web_root}")
    app = create_app(Settings(host=LOOPBACK_HOST, port=DEFAULT_PORT, workspace=workspace))
    # FastAPI evaluates routes in registration order. The API is registered first; this final
    # catch-all mount serves the SPA without shadowing /api/*.
    app.mount("/", StaticFiles(directory=web_root, html=True), name="studio")
    return app


class InstanceLock(AbstractContextManager):
    """One server per user installation, with state useful for relaunch and diagnostics."""

    def __init__(
        self,
        paths: MacOSPaths,
        *,
        url: str,
        workspace: Path,
        version: str,
    ) -> None:
        self.paths = paths
        self.url = url
        self.workspace = workspace
        self.version = version
        self._stream = None
        self.acquired = False

    def __enter__(self) -> InstanceLock:
        self.paths.runtime.mkdir(parents=True, exist_ok=True)
        stream = (self.paths.runtime / "server.lock").open("a+", encoding="utf-8")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            return self
        self._stream = stream
        self.acquired = True
        _atomic_text(self.paths.runtime / "server.pid", f"{os.getpid()}\n")
        _atomic_json(
            self.paths.runtime / "server.json",
            {
                "pid": os.getpid(),
                "started_at": _utc_now(),
                "url": self.url,
                "version": self.version,
                "workspace": str(self.workspace),
            },
        )
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.acquired:
            (self.paths.runtime / "server.pid").unlink(missing_ok=True)
            (self.paths.runtime / "server.json").unlink(missing_ok=True)
        if self._stream is not None:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            self._stream.close()
        self.acquired = False


def installed_web_root(paths: MacOSPaths) -> Path:
    override = os.environ.get("IMAGE23MF_WEB_ROOT")
    return Path(override).expanduser().resolve() if override else paths.current / "web"


def configure_logging(paths: MacOSPaths) -> Path:
    paths.logs.mkdir(parents=True, exist_ok=True)
    log_path = paths.logs / "studio.log"
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S%z"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(
        isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename) == log_path
        for handler in root.handlers
    ):
        handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3)
        handler.setFormatter(formatter)
        root.addHandler(handler)
    log_path.chmod(0o600)
    return log_path


def read_running_url(paths: MacOSPaths, default: str) -> str:
    try:
        payload = json.loads((paths.runtime / "server.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    value = payload.get("url")
    return value if isinstance(value, str) and value.startswith("http://127.0.0.1:") else default


def open_when_ready(url: str, *, timeout_seconds: float = 15.0) -> None:
    health = f"{url}/api/health"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health, timeout=0.5) as response:
                if response.status < 500:
                    webbrowser.open(url)
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    LOGGER.warning("server did not become ready before browser-open timeout", extra={"url": url})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch the local Image23MF Studio app")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--home", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--web-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--migrate-only", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.port < 1 or args.port > 65535:
        raise SystemExit("--port must be between 1 and 65535")
    if args.migrate_only:
        if args.workspace is None:
            raise SystemExit("--migrate-only requires --workspace")
        # Staged-release migration is deliberately preference-free. In particular, an installer
        # exercising a release under a synthetic HOME must never rewrite the user's real config.
        migrate_workspace(args.workspace.expanduser().resolve())
        return 0
    paths = MacOSPaths.for_home(args.home)
    workspace = resolve_workspace(
        paths,
        explicit=args.workspace,
        chooser=choose_workspace,
    )
    migrate_workspace(workspace)

    configure_logging(paths)
    web_root = args.web_root.resolve() if args.web_root else installed_web_root(paths)
    url = f"http://{LOOPBACK_HOST}:{args.port}"
    with InstanceLock(paths, url=url, workspace=workspace, version=__version__) as instance:
        if not instance.acquired:
            running_url = read_running_url(paths, url)
            LOGGER.info("studio already running at %s", running_url)
            if not args.no_browser:
                webbrowser.open(running_url)
            return 0
        LOGGER.info(
            "starting Image23MF Studio %s at %s with workspace %s",
            __version__,
            url,
            workspace,
        )
        app = create_local_app(workspace, web_root)
        if not args.no_browser:
            threading.Thread(target=open_when_ready, args=(url,), daemon=True).start()
        import uvicorn

        uvicorn.run(
            app,
            host=LOOPBACK_HOST,
            port=args.port,
            access_log=False,
            log_config=None,
        )
    return 0


def _atomic_json(path: Path, payload: dict) -> None:
    _atomic_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
