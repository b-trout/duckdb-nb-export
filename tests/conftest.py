"""Shared pytest fixtures for notebook export tests.

Parameters
----------
None
    This module does not accept parameters.

Returns
-------
None
    Importing this module registers pytest fixtures.

Raises
------
None
    Importing this module should not raise package-specific exceptions.

Notes
-----
Only test infrastructure lives here; concrete test cases are authored
separately.
"""

from collections.abc import Generator
from pathlib import Path

import duckdb
import pytest


@pytest.fixture
def tmp_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a temporary current working directory for a test.

    Parameters
    ----------
    tmp_path
        Pytest temporary directory fixture.
    monkeypatch
        Pytest monkeypatch fixture used to change the current directory.

    Returns
    -------
    pathlib.Path
        Temporary directory that is also the current working directory.

    Raises
    ------
    OSError
        Raised if the current directory cannot be changed.

    Notes
    -----
    The directory is unique per test and is based on ``tmp_path``.
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def fresh_duckdb(tmp_path: Path) -> Generator[Path]:
    """Provide a path for a fresh, already-created temporary DuckDB database.

    Parameters
    ----------
    tmp_path
        Pytest temporary directory fixture.

    Returns
    -------
    collections.abc.Generator[pathlib.Path]
        Generator yielding an existing, empty DuckDB database file path.

    Raises
    ------
    None
        This fixture does not raise package-specific exceptions.

    Notes
    -----
    The file is pre-created (and immediately closed) so tests exercise
    ``execute_notebook`` against a real, already-existing target database,
    matching the ``--db`` contract that requires an existing file
    (``TargetDatabaseError`` otherwise; see issue #30).
    """
    path = tmp_path / "test.duckdb"
    duckdb.connect(str(path)).close()
    yield path
