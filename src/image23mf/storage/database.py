import sqlite3
from pathlib import Path
from typing import Union

from image23mf.storage.migrations import apply_migrations

DatabasePath = Union[str, Path]


def configure_connection(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")


def open_database(path: DatabasePath) -> sqlite3.Connection:
    database_path = Path(path).expanduser()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    # FastAPI may enter and close one synchronous yield dependency on different worker
    # threads. The connection remains request-scoped and is never used concurrently, so
    # allowing that sequential handoff is required outside TestClient.
    connection = sqlite3.connect(database_path, timeout=5, check_same_thread=False)
    try:
        configure_connection(connection)
        apply_migrations(connection)
    except Exception:
        connection.close()
        raise
    return connection
