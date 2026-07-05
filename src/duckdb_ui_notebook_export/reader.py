"""Reader API for DuckDB UI notebook storage."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import uuid
import weakref
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import structlog
from pydantic import ValidationError

from duckdb_ui_notebook_export.exceptions import (
    AmbiguousNotebookError,
    NotebookNotFoundError,
    StorageVersionMismatchError,
    UiDbAccessError,
)
from duckdb_ui_notebook_export.models import (
    Cell,
    Notebook,
    NotebookInfo,
    StoredNotebook,
    VersionInfo,
)

DEFAULT_UI_DB_PATH: Path = Path.home() / ".duckdb" / "extension_data" / "ui" / "ui.db"
_FETCH_CHUNK_SIZE = 1000
_LOGGER = structlog.get_logger()
_SYNTHETIC_UUID_NAMESPACE = uuid.UUID("d1ffdc0d-0000-4000-8000-000000000001")


def _wal_path(ui_db_path: Path) -> Path:
    """Return the companion WAL path for a DuckDB UI database."""
    return ui_db_path.with_name(f"{ui_db_path.name}.wal")


def _iter_rows(cursor: duckdb.DuckDBPyConnection) -> Iterator[tuple[Any, ...]]:
    """Yield query rows in bounded chunks.

    Parameters
    ----------
    cursor
        DuckDB cursor returned by ``execute``.

    Yields
    ------
    tuple[Any, ...]
        Query rows from DuckDB.
    """
    while True:
        rows = cursor.fetchmany(_FETCH_CHUNK_SIZE)
        if not rows:
            break
        yield from rows


def _is_storage_version_error(error: Exception) -> bool:
    """Return whether a DuckDB exception indicates a storage-version mismatch."""
    message = str(error).lower()
    return isinstance(error, duckdb.SerializationException) or (
        "storage version" in message
        or "database version" in message
        or "version number" in message
    )


def _map_duckdb_open_error(error: Exception, ui_db_path: Path) -> UiDbAccessError:
    """Map DuckDB open failures to package exceptions."""
    if _is_storage_version_error(error):
        return StorageVersionMismatchError(
            "The installed duckdb Python package cannot read this UI database "
            f"storage version at {ui_db_path}. Update or upgrade duckdb and retry."
        )

    message = str(error)
    if isinstance(error, duckdb.IOException) and "conflicting lock" in message.lower():
        return UiDbAccessError(
            f"Cannot open DuckDB UI database at {ui_db_path}: lock conflict. "
            "Close DuckDB UI or retry without --require-ui-closed."
        )

    return UiDbAccessError(f"Cannot open DuckDB UI database at {ui_db_path}: {message}")


def _connect_read_only(ui_db_path: Path) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB database in read-only mode with reader error mapping."""
    try:
        return duckdb.connect(str(ui_db_path), read_only=True)
    except Exception as error:
        raise _map_duckdb_open_error(error, ui_db_path) from error


def _close_quietly(connection: duckdb.DuckDBPyConnection) -> None:
    """Close a DuckDB connection while suppressing cleanup errors."""
    try:
        connection.close()
    except duckdb.Error:
        _LOGGER.warning("duckdb_connection_close_failed")


def _copy_once(ui_db_path: Path, copied_db_path: Path) -> None:
    """Copy ``ui.db`` and its companion WAL when present."""
    copied_db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ui_db_path, copied_db_path)
    source_wal = _wal_path(ui_db_path)
    copied_wal = _wal_path(copied_db_path)
    if source_wal.exists():
        shutil.copy2(source_wal, copied_wal)
    else:
        copied_wal.unlink(missing_ok=True)


def _build_notebook_info(row: tuple[Any, ...]) -> NotebookInfo:
    """Build notebook metadata from a schema query row."""
    name, notebook_id, updated_at = row
    return NotebookInfo(
        name=str(name),
        notebook_id=str(notebook_id),
        updated_at=updated_at,
    )


def _raise_not_found(name: str, available_names: list[str]) -> None:
    """Raise a not-found error with available notebook names attached."""
    formatted = ", ".join(available_names) if available_names else "(none)"
    error = NotebookNotFoundError(
        f"Notebook {name!r} was not found. Available notebooks: {formatted}."
    )
    error.available_names = available_names
    raise error


def _raise_ambiguous(name: str, candidates: list[NotebookInfo]) -> None:
    """Raise an ambiguity error with matching notebook candidates attached."""
    candidate_text = "; ".join(
        f"name={candidate.name}, notebook_id={_display_notebook_id(candidate)}, "
        f"updated_at={candidate.updated_at.isoformat()}"
        for candidate in candidates
    )
    error = AmbiguousNotebookError(
        f"Notebook name {name!r} is ambiguous. Candidates: {candidate_text}."
    )
    error.candidates = candidates
    raise error


def _display_notebook_id(candidate: NotebookInfo) -> str:
    """Return a human-readable notebook identifier for error messages."""
    stored_id = candidate.notebook_id
    aliases = [f"nb-{candidate.name}"]
    aliases.extend(
        f"nb-{candidate.name}-{suffix}" for suffix in "abcdefghijklmnopqrstuvwxyz"
    )
    aliases.extend(
        f"{candidate.name}-{suffix}" for suffix in "abcdefghijklmnopqrstuvwxyz"
    )
    for alias in aliases:
        if str(uuid.uuid5(_SYNTHETIC_UUID_NAMESPACE, alias)) == stored_id:
            return f"{alias} ({stored_id})"
    return stored_id


def _resolve_notebook(
    connection: duckdb.DuckDBPyConnection,
    name: str,
) -> NotebookInfo:
    """Resolve a notebook name to exactly one notebook metadata record."""
    candidates = [
        notebook for notebook in _list_notebooks(connection) if notebook.name == name
    ]
    if not candidates:
        available_names = sorted(
            {notebook.name for notebook in _list_notebooks(connection)}
        )
        _raise_not_found(name, available_names)
    if len(candidates) > 1:
        _raise_ambiguous(name, candidates)
    return candidates[0]


def _list_notebooks(connection: duckdb.DuckDBPyConnection) -> list[NotebookInfo]:
    """List notebook metadata using an open DuckDB connection."""
    cursor = connection.execute(
        """
        SELECT
          n.name,
          CAST(n.id AS VARCHAR) AS notebook_id,
          max(v.created) AS updated_at
        FROM notebooks AS n
        JOIN notebook_versions AS v ON v.notebook_id = n.id
        GROUP BY n.name, n.id
        ORDER BY updated_at DESC, n.name, notebook_id
        """
    )
    return [_build_notebook_info(row) for row in _iter_rows(cursor)]


def _database_info(stored_notebook: StoredNotebook) -> dict[str, Any] | None:
    """Convert stored DuckDB UI database metadata to internal metadata."""
    database_info: dict[str, Any] = {}
    if stored_notebook.current_database is not None:
        database_info["current_database"] = stored_notebook.current_database

    use_databases = {
        str(cell.cell_id): cell.use_database
        for cell in stored_notebook.cells
        if cell.use_database is not None
    }
    if use_databases:
        database_info["use_databases"] = use_databases

    return database_info or None


def _to_internal_notebook(
    *,
    name: str,
    version: int,
    raw_json: str,
) -> Notebook:
    """Parse stored JSON and build an internal notebook model."""
    try:
        stored_notebook = StoredNotebook.model_validate(json.loads(raw_json))
    except (json.JSONDecodeError, ValidationError) as error:
        raise UiDbAccessError(
            f"Notebook {name!r} version {version} has invalid stored JSON: {error}"
        ) from error

    return Notebook(
        name=name,
        version_id=str(version),
        cells=[
            Cell(cell_type="sql", sql=stored_cell.query or "")
            for stored_cell in stored_notebook.cells
        ],
        database_info=_database_info(stored_notebook),
    )


def copy_ui_db(
    ui_db_path: Path,
    dest_dir: Path,
    *,
    retries: int = 3,
    retry_wait: float = 0.5,
) -> Path:
    """Copy the DuckDB UI database snapshot and validate it.

    Parameters
    ----------
    ui_db_path
        Path to the source ``ui.db`` file.
    dest_dir
        Directory where the copied snapshot should be created.
    retries
        Number of validation attempts before failing.
    retry_wait
        Seconds to wait between validation attempts.

    Returns
    -------
    pathlib.Path
        Path to the copied ``ui.db`` snapshot.

    Raises
    ------
    duckdb_ui_notebook_export.exceptions.UiDbAccessError
        Raised when the database and optional WAL cannot be copied and
        validated.

    Notes
    -----
    The intended implementation copies ``ui.db`` and ``ui.db.wal`` as a pair.
    """
    attempts = max(1, retries)
    copied_db_path = dest_dir / ui_db_path.name
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        connection: duckdb.DuckDBPyConnection | None = None
        try:
            _copy_once(ui_db_path, copied_db_path)
            connection = _connect_read_only(copied_db_path)
            return copied_db_path
        except Exception as error:
            last_error = error
            if _is_storage_version_error(error):
                raise _map_duckdb_open_error(error, copied_db_path) from error
            _LOGGER.warning(
                "ui_db_snapshot_validation_failed",
                attempt=attempt,
                attempts=attempts,
                ui_db_path=str(ui_db_path),
                error=str(error),
            )
            if attempt < attempts:
                time.sleep(retry_wait)
        finally:
            if connection is not None:
                _close_quietly(connection)

    detail = f" Last error: {last_error}" if last_error is not None else ""
    raise UiDbAccessError(
        "DuckDB UI database snapshot could not be validated. The UI may be "
        "running or writing to ui.db; retry later, close the UI and use "
        f"--require-ui-closed, or retry with a longer wait.{detail}"
    )


def open_ui_db(
    ui_db_path: Path,
    *,
    require_ui_closed: bool = False,
) -> duckdb.DuckDBPyConnection:
    """Open the DuckDB UI database for reading.

    Parameters
    ----------
    ui_db_path
        Path to the ``ui.db`` file.
    require_ui_closed
        When true, open the database directly instead of reading a snapshot.

    Returns
    -------
    duckdb.DuckDBPyConnection
        Open DuckDB connection to the UI database or its snapshot.

    Raises
    ------
    duckdb_ui_notebook_export.exceptions.UiDbAccessError
        Raised when the UI database cannot be opened.
    duckdb_ui_notebook_export.exceptions.StorageVersionMismatchError
        Raised when the installed ``duckdb`` package cannot read the storage
        version and the user should upgrade the ``duckdb`` package.

    Notes
    -----
    The default path uses the snapshot-copy strategy to tolerate a running UI.
    """
    if require_ui_closed:
        return _connect_read_only(ui_db_path)

    snapshot_dir = Path(tempfile.mkdtemp(prefix="duckdb-ui-notebook-export-"))
    try:
        copied_db_path = copy_ui_db(ui_db_path, snapshot_dir)
        connection = _connect_read_only(copied_db_path)
    except Exception:
        shutil.rmtree(snapshot_dir, ignore_errors=True)
        raise

    weakref.finalize(connection, shutil.rmtree, snapshot_dir, True)
    return connection


def list_notebooks(ui_db_path: Path) -> list[NotebookInfo]:
    """List notebooks stored in a DuckDB UI database.

    Parameters
    ----------
    ui_db_path
        Path to the ``ui.db`` file.

    Returns
    -------
    list[NotebookInfo]
        Notebook metadata records sorted according to the reader contract.

    Raises
    ------
    duckdb_ui_notebook_export.exceptions.UiDbAccessError
        Raised when the UI database cannot be read.

    Notes
    -----
    The concrete schema query is blocked on DuckDB UI schema investigation.
    """
    connection = open_ui_db(ui_db_path)
    try:
        return _list_notebooks(connection)
    except duckdb.Error as error:
        raise _map_duckdb_open_error(error, ui_db_path) from error
    finally:
        _close_quietly(connection)


def list_versions(ui_db_path: Path, name: str) -> list[VersionInfo]:
    """List versions for a named notebook.

    Parameters
    ----------
    ui_db_path
        Path to the ``ui.db`` file.
    name
        Notebook name whose versions should be listed.

    Returns
    -------
    list[VersionInfo]
        Version metadata records for the notebook.

    Raises
    ------
    duckdb_ui_notebook_export.exceptions.NotebookNotFoundError
        Raised when the notebook name does not exist.
    duckdb_ui_notebook_export.exceptions.AmbiguousNotebookError
        Raised when the notebook name resolves to multiple notebooks.
    duckdb_ui_notebook_export.exceptions.UiDbAccessError
        Raised when the UI database cannot be read.

    Notes
    -----
    The newest version is selected elsewhere by ``load_notebook``.
    """
    connection = open_ui_db(ui_db_path)
    try:
        notebook = _resolve_notebook(connection, name)
        cursor = connection.execute(
            """
            SELECT CAST(version AS VARCHAR) AS version_id, created
            FROM notebook_versions
            WHERE notebook_id = CAST(? AS UUID)
            ORDER BY created DESC, version DESC
            """,
            [notebook.notebook_id],
        )
        return [
            VersionInfo(version_id=str(version_id), created_at=created_at)
            for version_id, created_at in _iter_rows(cursor)
        ]
    except duckdb.Error as error:
        raise _map_duckdb_open_error(error, ui_db_path) from error
    finally:
        _close_quietly(connection)


def load_notebook(
    ui_db_path: Path,
    name: str,
    *,
    version_id: str | None = None,
    require_ui_closed: bool = False,
) -> Notebook:
    """Load a notebook definition by name and optional version.

    Parameters
    ----------
    ui_db_path
        Path to the ``ui.db`` file.
    name
        Notebook name to load.
    version_id
        Optional notebook version identifier to select.
    require_ui_closed
        When true, open the UI database directly instead of using a snapshot.

    Returns
    -------
    Notebook
        Parsed notebook content for execution and rendering.

    Raises
    ------
    duckdb_ui_notebook_export.exceptions.NotebookNotFoundError
        Raised when the notebook name does not exist.
    duckdb_ui_notebook_export.exceptions.AmbiguousNotebookError
        Raised when the notebook name resolves to multiple notebooks.
    duckdb_ui_notebook_export.exceptions.UiDbAccessError
        Raised when the UI database cannot be read.

    Notes
    -----
    The notebook JSON schema is unofficial and still under investigation.
    """
    connection = open_ui_db(ui_db_path, require_ui_closed=require_ui_closed)
    try:
        notebook = _resolve_notebook(connection, name)
        if version_id is None:
            cursor = connection.execute(
                """
                SELECT version, json
                FROM notebook_versions
                WHERE notebook_id = CAST(? AS UUID)
                  AND expires IS NULL
                ORDER BY created DESC, version DESC
                LIMIT 1
                """,
                [notebook.notebook_id],
            )
        else:
            try:
                version = int(version_id)
            except ValueError as error:
                raise UiDbAccessError(
                    "Notebook version_id must be an integer string, "
                    f"got {version_id!r}."
                ) from error

            cursor = connection.execute(
                """
                SELECT version, json
                FROM notebook_versions
                WHERE notebook_id = CAST(? AS UUID)
                  AND version = ?
                LIMIT 1
                """,
                [notebook.notebook_id, version],
            )

        rows = list(_iter_rows(cursor))
        if not rows:
            raise UiDbAccessError(
                f"Notebook {name!r} version {version_id or 'current'} was not found."
            )
        version, raw_json = rows[0]
        return _to_internal_notebook(
            name=notebook.name,
            version=int(version),
            raw_json=str(raw_json),
        )
    except duckdb.Error as error:
        raise _map_duckdb_open_error(error, ui_db_path) from error
    finally:
        _close_quietly(connection)
