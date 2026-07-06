"""Exception and exit-code definitions for notebook export.

Parameters
----------
None
    This module does not accept parameters.

Returns
-------
None
    Importing this module exposes exception classes and exit-code constants.

Raises
------
None
    Importing this module should not raise package-specific exceptions.

Notes
-----
Exception behavior is intentionally minimal until the reader, executor, and CLI
implementations are driven by tests.
"""

import enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from duckdb_ui_notebook_export.models import NotebookInfo


class ExporterError(Exception):
    """Base exception for exporter failures.

    Parameters
    ----------
    *args
        Positional exception message arguments inherited from ``Exception``.

    Returns
    -------
    None
        Exception classes do not return values.

    Raises
    ------
    None
        Instantiating the base exception does not raise package-specific errors.

    Notes
    -----
    Concrete subclasses identify reader, executor, renderer, and CLI failures.
    """


class NotebookNotFoundError(ExporterError):
    """Raised when a requested notebook name cannot be found.

    Parameters
    ----------
    *args
        Positional exception message arguments inherited from ``Exception``.

    Returns
    -------
    None
        Exception classes do not return values.

    Raises
    ------
    None
        Instantiating the exception does not raise package-specific errors.

    Notes
    -----
    ``available_names`` is intended to contain known notebook names.
    """

    available_names: list[str]


class AmbiguousNotebookError(ExporterError):
    """Raised when a notebook name resolves to multiple candidates.

    Parameters
    ----------
    *args
        Positional exception message arguments inherited from ``Exception``.

    Returns
    -------
    None
        Exception classes do not return values.

    Raises
    ------
    None
        Instantiating the exception does not raise package-specific errors.

    Notes
    -----
    ``candidates`` is intended to contain matching notebook metadata records.
    """

    candidates: list["NotebookInfo"]


class UiDbAccessError(ExporterError):
    """Raised when the DuckDB UI database cannot be accessed.

    Parameters
    ----------
    *args
        Positional exception message arguments inherited from ``Exception``.

    Returns
    -------
    None
        Exception classes do not return values.

    Raises
    ------
    None
        Instantiating the exception does not raise package-specific errors.

    Notes
    -----
    This includes lock, copy, corruption, and storage-version failures.
    """


class StorageVersionMismatchError(UiDbAccessError):
    """Raised when DuckDB cannot open a newer storage version.

    Parameters
    ----------
    *args
        Positional exception message arguments inherited from ``Exception``.

    Returns
    -------
    None
        Exception classes do not return values.

    Raises
    ------
    None
        Instantiating the exception does not raise package-specific errors.

    Notes
    -----
    Messages for this exception must urge the user to upgrade the ``duckdb``
    Python package before retrying.
    """


class OutputPathError(ExporterError):
    """Raised when the requested output path is rejected.

    Parameters
    ----------
    *args
        Positional exception message arguments inherited from ``Exception``.

    Returns
    -------
    None
        Exception classes do not return values.

    Raises
    ------
    None
        Instantiating the exception does not raise package-specific errors.

    Notes
    -----
    Rejection is based on normalized path containment under an allowed base.
    """


class TargetDatabaseError(ExporterError):
    """Raised when ``--db`` cannot be used as a valid execution target.

    Parameters
    ----------
    *args
        Positional exception message arguments inherited from ``Exception``.

    Returns
    -------
    None
        Exception classes do not return values.

    Raises
    ------
    None
        Instantiating the exception does not raise package-specific errors.

    Notes
    -----
    Covers a plain local ``--db`` path that does not exist (a likely typo;
    DuckDB would otherwise silently create an empty database file there) and
    ``--read-only`` combined with a target that cannot be opened read-only
    (``:memory:`` or a nonexistent file).
    """


class ExitCode(enum.IntEnum):
    """Process exit codes for the command-line interface.

    Parameters
    ----------
    value
        Integer enum value supplied by ``enum.IntEnum``.

    Returns
    -------
    ExitCode
        The matching enum member.

    Raises
    ------
    ValueError
        Raised by ``enum.IntEnum`` when ``value`` is not a known member.

    Notes
    -----
    The numeric values mirror the design document CLI contract.
    """

    OK = 0
    NOTEBOOK_NOT_FOUND = 1
    CELL_ERROR = 2
    OUTPUT_PATH_REJECTED = 3
    UI_DB_ACCESS_FAILED = 4
    CONFIRMATION_DECLINED = 5
    EXECUTION_FAILED = 6
