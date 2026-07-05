"""Notebook execution API for DuckDB UI notebook export.

Parameters
----------
None
    This module does not accept parameters.

Returns
-------
None
    Importing this module exposes execution models and functions.

Raises
------
None
    Importing this module should not raise package-specific exceptions.

Notes
-----
Functions are intentionally unimplemented stubs for test-first development.
"""

import enum
from dataclasses import dataclass

from duckdb_ui_notebook_export.models import Notebook


class CellStatus(enum.Enum):
    """Execution status for a notebook cell.

    Parameters
    ----------
    value
        Enum value supplied by ``enum.Enum``.

    Returns
    -------
    CellStatus
        The matching enum member.

    Raises
    ------
    ValueError
        Raised by ``enum.Enum`` when ``value`` is not a known member.

    Notes
    -----
    Status values model successful, failed, skipped, timed-out, and rejected
    cells.
    """

    OK = "OK"
    ERROR = "ERROR"
    SKIPPED_ABORT = "SKIPPED_ABORT"
    TIMEOUT = "TIMEOUT"
    REJECTED_TRANSACTION_STATEMENT = "REJECTED_TRANSACTION_STATEMENT"


@dataclass
class CellResult:
    """Result captured for one executed notebook cell.

    Parameters
    ----------
    status
        Execution status for the cell.
    columns
        Result column names.
    rows
        Result rows.
    truncated
        Whether more rows existed than the configured display limit.
    affected_rows
        Number of affected rows for DML statements when available.
    error_message
        Error message for failed cells.

    Returns
    -------
    CellResult
        Dataclass instance containing a cell result.

    Raises
    ------
    TypeError
        Raised by dataclass construction when required arguments are missing.

    Notes
    -----
    Query result rows are intentionally represented as tuples for DuckDB
    compatibility.
    """

    status: CellStatus
    columns: list[str]
    rows: list[tuple]
    truncated: bool
    affected_rows: int | None
    error_message: str | None


@dataclass
class ExecutionReport:
    """Execution report for a complete notebook.

    Parameters
    ----------
    cell_results
        Ordered result objects, one per notebook cell.
    warnings
        Warning messages to surface in CLI output and HTML metadata.
    used_memory_fallback
        Whether ``:memory:`` was used because no target database was resolved.

    Returns
    -------
    ExecutionReport
        Dataclass instance containing notebook execution results.

    Raises
    ------
    TypeError
        Raised by dataclass construction when required arguments are missing.

    Notes
    -----
    The report is consumed by the renderer and CLI layers.
    """

    cell_results: list[CellResult]
    warnings: list[str]
    used_memory_fallback: bool


def resolve_target_db(notebook: Notebook, cli_db: str | None) -> tuple[str, bool]:
    """Resolve the target DuckDB database for notebook execution.

    Parameters
    ----------
    notebook
        Notebook whose JSON metadata may contain connection information.
    cli_db
        Database path supplied by ``--db``.

    Returns
    -------
    tuple[str, bool]
        Database path, or ``":memory:"``, and whether memory fallback was used.

    Raises
    ------
    NotImplementedError
        Always raised until implementation is driven by tests.

    Notes
    -----
    Resolution priority is ``--db``, notebook JSON, then ``:memory:`` per
    ADR-008.
    """
    raise NotImplementedError


def contains_transaction_statement(sql: str) -> bool:
    """Detect transaction control statements in SQL text.

    Parameters
    ----------
    sql
        SQL text to inspect.

    Returns
    -------
    bool
        True when BEGIN, COMMIT, or ROLLBACK appears as a statement.

    Raises
    ------
    NotImplementedError
        Always raised until implementation is driven by tests.

    Notes
    -----
    The intended implementation must avoid false positives in string literals
    and comments by using SQL parsing.
    """
    raise NotImplementedError


def execute_notebook(
    notebook: Notebook,
    db: str,
    *,
    allow_writes: bool = False,
    max_rows: int = 1000,
    cell_timeout: float = 300.0,
    interrupt_grace: float = 30.0,
    stop_on_error: bool = False,
    no_external_access: bool = False,
) -> ExecutionReport:
    """Execute notebook cells against a DuckDB database.

    Parameters
    ----------
    notebook
        Notebook to execute.
    db
        Target DuckDB database path or ``":memory:"``.
    allow_writes
        Commit changes instead of rolling them back.
    max_rows
        Maximum result rows to include per cell.
    cell_timeout
        Per-cell timeout in seconds.
    interrupt_grace
        Seconds to wait after interrupt before abandoning remaining cells.
    stop_on_error
        Stop execution after the first failed cell.
    no_external_access
        Disable DuckDB external access during execution.

    Returns
    -------
    ExecutionReport
        Results and warnings for the executed notebook.

    Raises
    ------
    NotImplementedError
        Always raised until implementation is driven by tests.

    Notes
    -----
    The intended implementation uses one transaction and rolls back by default.
    """
    raise NotImplementedError
