from __future__ import annotations

import json
import stat
from pathlib import Path

from fastapi.testclient import TestClient

from image23mf.macos.launcher import (
    LOOPBACK_HOST,
    InstanceLock,
    create_local_app,
    main,
    migrate_workspace,
    read_running_url,
    resolve_workspace,
)
from image23mf.macos.lifecycle import MacOSPaths, load_workspace
from image23mf.storage import CURRENT_DATABASE_VERSION, open_database


def test_workspace_resolution_precedence_and_persistence(tmp_path: Path) -> None:
    paths = MacOSPaths.for_home(tmp_path / "home")
    chosen = resolve_workspace(
        paths,
        environment={},
        chooser=lambda: tmp_path / "chosen",
    )
    assert chosen == (tmp_path / "chosen").resolve()
    assert load_workspace(paths) == chosen

    from_environment = resolve_workspace(
        paths,
        environment={"IMAGE23MF_WORKSPACE": str(tmp_path / "environment")},
        chooser=lambda: tmp_path / "ignored",
    )
    assert from_environment == (tmp_path / "environment").resolve()

    explicit = resolve_workspace(
        paths,
        explicit=tmp_path / "explicit",
        environment={"IMAGE23MF_WORKSPACE": str(tmp_path / "ignored")},
    )
    assert explicit == (tmp_path / "explicit").resolve()
    assert load_workspace(paths) == explicit


def test_workspace_resolution_has_safe_documents_fallback(tmp_path: Path) -> None:
    paths = MacOSPaths.for_home(tmp_path / "home")
    resolved = resolve_workspace(paths, environment={}, chooser=lambda: None)
    assert resolved == (paths.home / "Documents" / "Image23MF Studio").resolve()


def test_migrate_only_opens_database_at_current_schema(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    migrate_workspace(workspace)
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    finally:
        connection.close()
    assert version == CURRENT_DATABASE_VERSION


def test_migrate_only_does_not_read_or_write_launcher_preferences(tmp_path: Path) -> None:
    home = tmp_path / "synthetic-home"
    workspace = tmp_path / "staged-workspace"

    assert (
        main(
            [
                "--home",
                str(home),
                "--migrate-only",
                "--workspace",
                str(workspace),
            ]
        )
        == 0
    )

    assert (workspace / "image23mf.sqlite3").is_file()
    assert not MacOSPaths.for_home(home).config.exists()


def test_local_app_serves_api_and_frontend_from_one_loopback_origin(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<h1>installed studio</h1>", encoding="utf-8")
    app = create_local_app(workspace, web)

    with TestClient(app) as client:
        health = client.get("/api/health")
        frontend = client.get("/")

    assert health.status_code == 200
    assert health.json()["workspace"] == str(workspace)
    assert frontend.status_code == 200
    assert frontend.text == "<h1>installed studio</h1>"


def test_instance_lock_records_version_logs_workspace_and_relaunch_url(tmp_path: Path) -> None:
    paths = MacOSPaths.for_home(tmp_path / "home")
    workspace = tmp_path / "workspace"
    url = f"http://{LOOPBACK_HOST}:9123"

    with InstanceLock(paths, url=url, workspace=workspace, version="7.8.9") as first:
        assert first.acquired
        state = json.loads((paths.runtime / "server.json").read_text(encoding="utf-8"))
        assert state["version"] == "7.8.9"
        assert state["workspace"] == str(workspace)
        assert read_running_url(paths, "fallback") == url
        assert stat.S_IMODE((paths.runtime / "server.pid").stat().st_mode) == 0o600
        assert stat.S_IMODE((paths.runtime / "server.json").stat().st_mode) == 0o600
        with InstanceLock(paths, url=url, workspace=workspace, version="7.8.9") as second:
            assert not second.acquired

    assert not (paths.runtime / "server.pid").exists()
    assert not (paths.runtime / "server.json").exists()


def test_launcher_always_binds_uvicorn_to_loopback(tmp_path: Path, monkeypatch) -> None:
    import uvicorn

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("studio", encoding="utf-8")
    observed: dict = {}

    def fake_run(app, **kwargs) -> None:
        observed["app"] = app
        observed.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)

    exit_code = main(
        [
            "--home",
            str(home),
            "--workspace",
            str(workspace),
            "--web-root",
            str(web),
            "--port",
            "9456",
            "--no-browser",
        ]
    )

    assert exit_code == 0
    assert observed["host"] == "127.0.0.1"
    assert observed["port"] == 9456
    assert not (MacOSPaths.for_home(home).runtime / "server.pid").exists()
    assert stat.S_IMODE((MacOSPaths.for_home(home).logs / "studio.log").stat().st_mode) == 0o600
