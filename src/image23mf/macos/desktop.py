"""Authenticated, frozen engine owned by the native desktop window.

The session secret arrives over a private pipe, never in argv, a URL or a log.
Only loopback is bound, on an OS-assigned port. No developer Python is needed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import signal
import socket
import sys
import threading
import time
from contextlib import suppress
from http.cookies import CookieError, SimpleCookie
from pathlib import Path

COOKIE_NAME = "image23mf_desktop"


class DesktopBoundary:
    """Keep other websites/local clients outside the desktop session."""

    def __init__(self, app, *, token: str, port: int):
        self.app = app
        self.token_hash = hashlib.sha256(token.encode()).digest()
        self.host = f"127.0.0.1:{port}"
        self.origin = f"http://{self.host}"

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get(b"cookie", b"").decode("latin1"))
            supplied = cookie[COOKIE_NAME].value
        except (CookieError, KeyError, ValueError):
            supplied = ""
        valid = (
            scope["type"] == "http"
            and headers.get(b"host", b"").decode("latin1") == self.host
            and headers.get(b"origin", self.origin.encode()).decode("latin1") == self.origin
            and headers.get(b"sec-fetch-site", b"none") in (b"none", b"same-origin")
            and hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(), self.token_hash)
        )
        if not valid:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await send({"type": "http.response.start", "status": 403, "headers": []})
                await send({"type": "http.response.body", "body": b"Desktop session required."})
            return

        async def secure_send(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (
                        b"content-security-policy",
                        b"frame-ancestors 'none'; object-src 'none'; base-uri 'self'",
                    ),
                ]
            await send(message)

        await self.app(scope, receive, secure_send)


def read_configuration(stream) -> dict:
    config = json.loads(stream.readline(65537))
    if not isinstance(config, dict) or not re.fullmatch(r"[a-f0-9]{64}", config.get("token", "")):
        raise ValueError("Invalid desktop session configuration.")
    for key in ("workspace", "home", "web_root"):
        if not isinstance(config.get(key), str) or not Path(config[key]).is_absolute():
            raise ValueError(f"Desktop {key} must be an absolute path.")
    return config


def run(config: dict) -> int:
    # Import after freeze_support has dispatched multiprocessing children.
    import uvicorn
    from fastapi.staticfiles import StaticFiles

    from image23mf.macos.launcher import InstanceLock, configure_logging
    from image23mf.macos.lifecycle import (
        DATABASE_FILENAME,
        MacOSPaths,
        backup_workspace_database,
        restore_workspace_database,
    )

    workspace = Path(config["workspace"]).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    web_root = Path(config["web_root"]).resolve()
    if not (web_root / "index.html").is_file():
        raise ValueError("The application interface is missing. Reinstall the app.")
    paths = MacOSPaths.for_home(Path(config["home"]))
    configure_logging(paths)
    # Imports in the engine use Settings; isolate them from the launcher's cwd.
    if getattr(sys, "frozen", False):
        bundled_tools = Path(sys.executable).resolve().parent.parent / "tools"
        if not (bundled_tools / "potrace").is_file():
            raise ValueError("The bundled vectorizer is missing. Reinstall the app.")
        os.environ["PATH"] = str(bundled_tools) + os.pathsep + "/usr/bin:/bin:/usr/sbin:/sbin"
    os.environ["IMAGE23MF_WORKSPACE"] = str(workspace)
    os.chdir(workspace)
    parent = os.getppid()
    # Foundation may already have assigned a private process group.
    with suppress(PermissionError):
        os.setsid()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        from image23mf import __version__

        with InstanceLock(paths, url=url, workspace=workspace, version=__version__) as lock:
            if not lock.acquired:
                raise RuntimeError("Image23MF is already running. Quit its other instance first.")
            # Always back up an existing database before a new application starts migrations.
            backup = backup_workspace_database(
                paths=paths, workspace=workspace, version=__version__
            )
            database_existed = (workspace / DATABASE_FILENAME).exists()
            try:
                from image23mf.api.app import create_app
                from image23mf.settings import Settings

                app = create_app(Settings(workspace=workspace, port=port, _env_file=None))
            except Exception:
                if backup is not None:
                    restore_workspace_database(workspace, backup)
                elif not database_existed:
                    for suffix in ("", "-wal", "-shm"):
                        (workspace / (DATABASE_FILENAME + suffix)).unlink(missing_ok=True)
                raise

            @app.get("/__desktop/status")
            def desktop_status():
                from image23mf.storage import open_database

                connection = open_database(workspace / DATABASE_FILENAME)
                try:
                    active = connection.execute(
                        "SELECT COUNT(*) FROM jobs WHERE state IN ('queued', 'running')"
                    ).fetchone()[0]
                    return {"active_jobs": active, "version": __version__}
                finally:
                    connection.close()

            app.mount("/", StaticFiles(directory=web_root, html=True), name="desktop")
            protected = DesktopBoundary(app, token=config["token"], port=port)
            server = uvicorn.Server(
                uvicorn.Config(
                    protected,
                    host="127.0.0.1",
                    port=port,
                    access_log=False,
                    log_config=None,
                    loop="asyncio",
                    http="h11",
                    ws="none",
                    timeout_graceful_shutdown=8,
                )
            )

            def supervise():
                announced = False
                while not server.should_exit:
                    if os.getppid() != parent:
                        server.should_exit = True
                        return
                    if server.started and not announced:
                        print(json.dumps({"ready": True, "url": url}), flush=True)
                        announced = True
                    time.sleep(0.1)

            threading.Thread(target=supervise, daemon=True).start()
            # Uvicorn re-raises termination signals after graceful shutdown. Keep that
            # re-raise from bypassing our lock/state cleanup with the default SIGTERM action.
            previous = signal.signal(
                signal.SIGTERM, lambda *_: setattr(server, "should_exit", True)
            )
            try:
                server.run(sockets=[listener])
            finally:
                signal.signal(signal.SIGTERM, previous)
            return 0


def main() -> int:
    try:
        return run(read_configuration(sys.stdin))
    except Exception as error:
        # Never echo config or session tokens. Native shell shows a useful startup error.
        print(json.dumps({"error": str(error)}), flush=True)
        return 1
