"""Reader API for DuckDB UI notebook storage.

Parameters
----------
None
    This module does not accept parameters.

Returns
-------
None
    Importing this module exposes reader constants and functions.

Raises
------
None
    Importing this module should not raise package-specific exceptions.

Notes
-----
Functions are intentionally unimplemented stubs for test-first development.
"""

from pathlib import Path

import duckdb

from duckdb_ui_notebook_export.models import Notebook, NotebookInfo, VersionInfo

DEFAULT_UI_DB_PATH: Path = Path.home() / ".duckdb" / "extension_data" / "ui" / "ui.db"


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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError
