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
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import structlog

from duckdb_ui_notebook_export.exceptions import TargetDatabaseError
from duckdb_ui_notebook_export.models import Notebook

LOGGER = structlog.get_logger()
TRANSACTION_STATEMENT_TYPES = {duckdb.StatementType.TRANSACTION}
DML_STATEMENT_TYPES = {
    duckdb.StatementType.INSERT,
    duckdb.StatementType.UPDATE,
    duckdb.StatementType.DELETE,
}
_URI_SCHEME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]+:")


class _CellInterrupted(KeyboardInterrupt):
    """A ``KeyboardInterrupt`` received while a cell's worker thread was running.

    Parameters
    ----------
    worker_exited
        Whether the worker thread had already returned (or could be
        interrupted and returned within ``interrupt_grace``) by the time
        this exception is raised.

    Returns
    -------
    _CellInterrupted
        Exception instance carrying ``worker_exited``.

    Raises
    ------
    None
        Construction does not raise package-specific exceptions.

    Notes
    -----
    Subclasses ``KeyboardInterrupt`` so that any code unaware of this type
    (for example a bare ``except KeyboardInterrupt`` elsewhere) still
    observes an interrupt rather than an unexpected exception type. The
    ``worker_exited`` flag lets ``execute_notebook`` decide whether it is
    safe to issue ``ROLLBACK``/``close()`` on the connection (issue #57):
    when the worker thread is still alive, DuckDB serializes operations per
    connection, so touching the connection again could block forever.
    """

    def __init__(self, *, worker_exited: bool) -> None:
        super().__init__()
        self.worker_exited = worker_exited


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
    abandoned
        Whether execution was abandoned after an uninterruptible timeout left
        a worker thread still running against the connection. When True, the
        connection was deliberately never touched again (no COMMIT/ROLLBACK,
        no ``close()``) because DuckDB serializes operations per connection
        and doing so could block forever.

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
    abandoned: bool = False


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
    None
        This function does not raise package-specific exceptions.

    Notes
    -----
    Resolution priority is ``--db``, notebook JSON, then ``:memory:`` per
    ADR-008. Stored format v3 keeps only a database name, not a file path, so
    the ``database_info["path"]`` branch below targets a future format
    extension or programmatic use (direct ``Notebook`` construction) rather
    than the current reader path; going through the reader currently always
    falls back to ``:memory:`` unless ``--db`` is supplied.
    """
    if cli_db is not None:
        return cli_db, False
    if notebook.database_info is not None:
        path = notebook.database_info.get("path")
        if isinstance(path, str) and path:
            return path, False
    return ":memory:", True


def _requires_existence_check(db: str) -> bool:
    """Return whether ``db`` is a plain local path that must already exist.

    Parameters
    ----------
    db
        Target database string as passed to ``duckdb.connect``.

    Returns
    -------
    bool
        True when ``db`` is a plain local filesystem path (not ``:memory:``
        and not a URI-style connect string), meaning a missing file is
        almost certainly a typo rather than an intentional new database.

    Raises
    ------
    None
        This function does not raise package-specific exceptions.

    Notes
    -----
    ``:memory:`` is never a local path. A string matching a URI scheme
    prefix (``^[A-Za-z][A-Za-z0-9+.-]+:``, i.e. 2+ characters before the
    colon) is treated as a connect string such as ``md:...`` or ``s3://...``
    and skipped, so those keep DuckDB's own connection semantics. A single
    letter followed by ``:`` (e.g. a Windows drive letter like ``C:\\...``)
    does not match that pattern and is still treated as a local path.
    """
    if db == ":memory:":
        return False
    return _URI_SCHEME_PATTERN.match(db) is None


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
    None
        Parse errors are intentionally treated as non-transaction SQL so
        execution can surface the natural DuckDB error.

    Notes
    -----
    The intended implementation must avoid false positives in string literals
    and comments by using SQL parsing.
    """
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error:
        return False
    return any(
        statement.type in TRANSACTION_STATEMENT_TYPES for statement in statements
    )


def _empty_result(status: CellStatus, error_message: str | None = None) -> CellResult:
    """Build a result object without rows.

    Parameters
    ----------
    status
        Cell execution status.
    error_message
        Optional failure explanation.

    Returns
    -------
    CellResult
        Empty result with the requested status.
    """
    return CellResult(
        status=status,
        columns=[],
        rows=[],
        truncated=False,
        affected_rows=None,
        error_message=error_message,
    )


def _fetch_limited_rows(
    cursor: duckdb.DuckDBPyConnection,
    max_rows: int,
) -> tuple[list[tuple], bool]:
    """Fetch at most ``max_rows + 1`` rows from a DuckDB cursor.

    Parameters
    ----------
    cursor
        Cursor returned by DuckDB execution.
    max_rows
        Maximum number of display rows to retain.

    Returns
    -------
    tuple[list[tuple], bool]
        Display rows and whether an additional row was available.
    """
    remaining = max_rows + 1
    collected: list[tuple] = []
    while remaining > 0:
        batch = cursor.fetchmany(remaining)
        if not batch:
            break
        collected.extend(batch)
        remaining -= len(batch)
    truncated = len(collected) > max_rows
    return collected[:max_rows], truncated


def _is_count_result(cursor: duckdb.DuckDBPyConnection) -> bool:
    """Return whether DuckDB exposed a single ``Count`` column.

    Parameters
    ----------
    cursor
        Cursor returned by DuckDB execution.

    Returns
    -------
    bool
        True when the result shape is DuckDB's DML count result.
    """
    return (
        cursor.description is not None
        and len(cursor.description) == 1
        and cursor.description[0][0] == "Count"
    )


def _result_from_cursor(
    cursor: duckdb.DuckDBPyConnection,
    statement_type: Any,
    max_rows: int,
) -> CellResult:
    """Convert the last DuckDB statement result to a cell result.

    Parameters
    ----------
    cursor
        Cursor returned by DuckDB execution.
    statement_type
        Parsed DuckDB statement type for the executed statement.
    max_rows
        Maximum display rows.

    Returns
    -------
    CellResult
        Successful cell result.
    """
    if cursor.description is None:
        return _empty_result(CellStatus.OK)

    if statement_type in DML_STATEMENT_TYPES and _is_count_result(cursor):
        rows, _ = _fetch_limited_rows(cursor, 1)
        affected_rows = int(rows[0][0]) if rows else 0
        return CellResult(
            status=CellStatus.OK,
            columns=[],
            rows=[],
            truncated=False,
            affected_rows=affected_rows,
            error_message=None,
        )

    columns = [column[0] for column in cursor.description]
    rows, truncated = _fetch_limited_rows(cursor, max_rows)
    return CellResult(
        status=CellStatus.OK,
        columns=columns,
        rows=rows,
        truncated=truncated,
        affected_rows=None,
        error_message=None,
    )


def _execute_cell(
    connection: duckdb.DuckDBPyConnection,
    sql: str,
    max_rows: int,
) -> CellResult:
    """Execute one SQL cell and capture only the last statement result.

    Parameters
    ----------
    connection
        Dedicated DuckDB connection used by the export.
    sql
        SQL text to execute.
    max_rows
        Maximum display rows.

    Returns
    -------
    CellResult
        Successful cell result.
    """
    statements = duckdb.extract_statements(sql)
    if not statements:
        return _empty_result(CellStatus.OK)

    cursor: duckdb.DuckDBPyConnection | None = None
    statement_type: Any = None
    for statement in statements:
        cursor = connection.execute(statement)
        statement_type = statement.type

    if cursor is None:
        return _empty_result(CellStatus.OK)
    return _result_from_cursor(cursor, statement_type, max_rows)


def _run_cell_in_thread(
    connection: duckdb.DuckDBPyConnection,
    sql: str,
    max_rows: int,
    cell_timeout: float,
    interrupt_grace: float,
) -> tuple[CellResult | None, BaseException | None, bool]:
    """Run a cell in a worker thread with interrupt-based timeout handling.

    Parameters
    ----------
    connection
        Dedicated DuckDB connection used by the export.
    sql
        SQL text to execute.
    max_rows
        Maximum display rows.
    cell_timeout
        Seconds to wait before interrupting the cell.
    interrupt_grace
        Seconds to wait for the interrupted worker to return.

    Returns
    -------
    tuple[CellResult | None, BaseException | None, bool]
        Result, exception, and whether execution was abandoned after timeout.

    Raises
    ------
    _CellInterrupted
        Raised when ``KeyboardInterrupt`` (e.g. Ctrl-C / SIGINT) arrives
        while waiting on the worker thread. ``connection.interrupt()`` is
        attempted first and the worker is given ``interrupt_grace`` seconds
        to return before this is raised, so ``worker_exited`` reports
        whether it is safe for the caller to touch the connection again
        (issue #57).

    Notes
    -----
    ``KeyboardInterrupt`` can only land here (rather than in the worker
    thread itself) because ``thread.join()`` is where the main thread is
    blocked; Python delivers signals to the main thread only.
    """
    state: dict[str, CellResult | BaseException] = {}

    def worker() -> None:
        try:
            state["result"] = _execute_cell(connection, sql, max_rows)
        except BaseException as error:
            state["error"] = error

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        thread.join(cell_timeout)
    except KeyboardInterrupt:
        _interrupt_and_raise(connection, thread, interrupt_grace)

    if thread.is_alive():
        connection.interrupt()
        try:
            thread.join(interrupt_grace)
        except KeyboardInterrupt:
            _interrupt_and_raise(connection, thread, interrupt_grace)
        if thread.is_alive():
            return None, None, True
        return (
            _empty_result(
                CellStatus.TIMEOUT,
                "Cell execution exceeded the timeout and was interrupted.",
            ),
            None,
            False,
        )

    result = state.get("result")
    error = state.get("error")
    return (
        result if isinstance(result, CellResult) else None,
        error if isinstance(error, BaseException) else None,
        False,
    )


def _interrupt_and_raise(
    connection: duckdb.DuckDBPyConnection,
    thread: threading.Thread,
    interrupt_grace: float,
) -> None:
    """Interrupt the connection, give the worker a grace period, then re-raise.

    Parameters
    ----------
    connection
        Dedicated DuckDB connection used by the export.
    thread
        Worker thread executing the current cell.
    interrupt_grace
        Seconds to wait for the interrupted worker to return.

    Returns
    -------
    None
        This function never returns normally.

    Raises
    ------
    _CellInterrupted
        Always raised, carrying whether the worker thread exited within
        ``interrupt_grace`` after ``connection.interrupt()``.
    """
    connection.interrupt()
    thread.join(interrupt_grace)
    raise _CellInterrupted(worker_exited=not thread.is_alive())


def _transaction_is_aborted(connection: duckdb.DuckDBPyConnection) -> bool:
    """Probe whether the active DuckDB transaction is aborted.

    Parameters
    ----------
    connection
        Dedicated DuckDB connection used by the export.

    Returns
    -------
    bool
        True when DuckDB reports a transaction-level failure.
    """
    try:
        connection.execute("SELECT 1")
    except duckdb.TransactionException:
        return True
    return False


def _append_skipped_abort_results(
    cell_results: list[CellResult],
    count: int,
    message: str,
) -> None:
    """Append skipped results for cells that cannot be executed.

    Parameters
    ----------
    cell_results
        Mutable result list.
    count
        Number of skipped cells to append.
    message
        Skip explanation.

    Returns
    -------
    None
        The result list is mutated in place.
    """
    for _ in range(count):
        cell_results.append(_empty_result(CellStatus.SKIPPED_ABORT, message))


def _notebook_current_database(notebook: Notebook) -> str | None:
    """Return the notebook-level database name from notebook metadata.

    Parameters
    ----------
    notebook
        Notebook whose ``database_info`` may name a current database.

    Returns
    -------
    str | None
        Database name stored as ``currentDatabase``, or None.
    """
    info = notebook.database_info or {}
    value = info.get("current_database")
    if isinstance(value, str) and value:
        return value
    return None


def _apply_use_database(
    connection: duckdb.DuckDBPyConnection,
    database_name: str,
    warnings: list[str],
    failed_databases: set[str],
) -> None:
    """Switch the connection's default database with a best-effort ``USE``.

    Parameters
    ----------
    connection
        Dedicated DuckDB connection used by the export.
    database_name
        Database (catalog) name recorded in the notebook JSON.
    warnings
        Mutable warning list surfaced in the report and rendered HTML.
    failed_databases
        Names that already failed once. The ``USE`` attempt itself is still
        retried for every cell (an earlier cell may ATTACH the name later),
        but a name already in this set does not emit a second warning while
        it keeps failing.

    Returns
    -------
    None
        On failure the connection keeps its current default database.

    Notes
    -----
    Stored format v3 records database names only (design doc 6.3#9), so this
    replay is best effort by design (ADR-008): the name resolves only when a
    matching catalog is attached, e.g. via ``--db`` or an earlier ATTACH cell.
    A failed ``USE`` raises ``CatalogException`` without aborting the
    transaction, so execution continues against the current database.
    """
    quoted = database_name.replace('"', '""')
    try:
        connection.execute(f'USE "{quoted}"')
    except duckdb.Error as error:
        already_failed = database_name in failed_databases
        failed_databases.add(database_name)
        if not already_failed:
            warning = (
                f"Could not switch to notebook database {database_name!r}; "
                f"continuing with the current database. Pass --db or ATTACH the "
                f"database in an earlier cell. ({error})"
            )
            warnings.append(warning)
            LOGGER.warning(
                "use_database_failed",
                database=database_name,
                error=str(error),
            )
    else:
        failed_databases.discard(database_name)


def _current_database_name(connection: duckdb.DuckDBPyConnection) -> str:
    """Return the connection's current default catalog name.

    Parameters
    ----------
    connection
        Dedicated DuckDB connection used by the export.

    Returns
    -------
    str
        Name reported by ``SELECT current_database()``.

    Raises
    ------
    duckdb.Error
        Propagated when the probe query itself fails.
    RuntimeError
        Raised if DuckDB unexpectedly returns no row for the probe query.
    """
    row = connection.execute("SELECT current_database()").fetchone()
    if row is None:
        message = "SELECT current_database() unexpectedly returned no row."
        raise RuntimeError(message)
    return row[0]


def _restart_transaction(connection: duckdb.DuckDBPyConnection) -> None:
    """Start a fresh transaction after aborting the current one.

    Parameters
    ----------
    connection
        Dedicated DuckDB connection used by the export.

    Returns
    -------
    None
        The current transaction is rolled back and a new one is opened.
    """
    connection.execute("ROLLBACK")
    connection.execute("BEGIN TRANSACTION")


def _restore_default_database_if_invalid(
    connection: duckdb.DuckDBPyConnection,
    primary_database: str,
    warnings: list[str],
) -> None:
    """Restore the primary catalog if the current default is no longer valid.

    Parameters
    ----------
    connection
        Dedicated DuckDB connection used by the export.
    primary_database
        Catalog name the connection defaulted to before ``BEGIN``.
    warnings
        Mutable warning list surfaced in the report and rendered HTML.

    Returns
    -------
    None
        The connection's default database is left untouched when it is
        still valid, otherwise best-effort restored to ``primary_database``.

    Notes
    -----
    A timeout-abort recovery calls ``ROLLBACK`` (ADR-007), which undoes a
    transaction-scoped ``ATTACH`` but does not reset the connection's
    default catalog if a cell had switched to it with ``USE``. That leaves
    the default catalog pointing at a database that no longer exists, so
    this probes with ``SELECT current_database()`` and restores the
    primary catalog on failure (ADR-008).
    """
    try:
        connection.execute("SELECT current_database()")
        return
    except duckdb.Error:
        pass

    quoted = primary_database.replace('"', '""')
    warning = (
        f"Default database was reset to {primary_database!r} because the "
        f"previously selected database is no longer attached after a "
        f"timeout rollback."
    )
    try:
        connection.execute(f'USE "{quoted}"')
    except duckdb.Error as error:
        warnings.append(
            f"{warning} Restoring the primary database also failed: {error}",
        )
        LOGGER.warning(
            "restore_default_database_failed",
            database=primary_database,
            error=str(error),
        )
        return

    warnings.append(warning)
    LOGGER.warning("default_database_reset_after_timeout", database=primary_database)


def execute_notebook(
    notebook: Notebook,
    db: str,
    *,
    allow_writes: bool = False,
    read_only: bool = False,
    max_rows: int = 1000,
    cell_timeout: float = 300.0,
    interrupt_grace: float = 30.0,
    stop_on_error: bool = False,
    no_external_access: bool = False,
    used_memory_fallback: bool | None = None,
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
    read_only
        Open the target database with DuckDB's read-only mode for a
        stronger no-writes guarantee than the default rollback-based
        safety net. Cells that create or modify tables fail with
        ``CellStatus.ERROR`` instead of being rolled back after the fact.
        Mutually exclusive with a ``":memory:"`` target, which DuckDB
        cannot open read-only.
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
    used_memory_fallback
        Whether ``db`` is ``":memory:"`` because no target database could be
        resolved (as reported by ``resolve_target_db``), as opposed to an
        explicit ``--db :memory:``. When True, the "no target database was
        resolved" warning is emitted; when False, it is suppressed even
        though ``db == ":memory:"`` (issue #49). When ``None`` (the
        default), this parameter falls back to the legacy behavior of
        recomputing the flag as ``db == ":memory:"``, which keeps direct
        callers that do not pass this keyword working exactly as before.

    Returns
    -------
    ExecutionReport
        Results and warnings for the executed notebook.

    Raises
    ------
    duckdb_ui_notebook_export.exceptions.TargetDatabaseError
        Raised when ``db`` is a plain local path that does not exist as a
        file, which is almost always a mistyped ``--db`` rather than an
        intentional new database (issue #30); also raised when
        ``read_only`` is True and ``db`` is ``":memory:"``, which DuckDB
        cannot open read-only (issue #31).
    duckdb.Error
        Raised when setup or final transaction control fails outside cell
        execution.

    Notes
    -----
    The intended implementation uses one transaction and rolls back by default.
    """
    warnings: list[str] = []
    if used_memory_fallback is None:
        used_memory_fallback = db == ":memory:"
    if used_memory_fallback:
        warning = "No target database was resolved; executing against :memory:."
        warnings.append(warning)
        LOGGER.warning("using_memory_database_fallback", database=db, warning=warning)

    if read_only and db == ":memory:":
        message = (
            "--read-only requires an existing database file; ':memory:' "
            "cannot be opened read-only."
        )
        raise TargetDatabaseError(message)

    if _requires_existence_check(db) and not Path(db).is_file():
        message = (
            f"Target database {db!r} does not exist. --db must point to an "
            f"existing DuckDB database file."
        )
        raise TargetDatabaseError(message)

    cell_results: list[CellResult] = []
    connection = duckdb.connect(db, read_only=read_only)
    abandoned = False
    commit_impossible = False
    try:
        primary_database = _current_database_name(connection)
        if no_external_access:
            connection.execute("SET enable_external_access=false")
        connection.execute("BEGIN TRANSACTION")

        failed_use_databases: set[str] = set()
        current_database = _notebook_current_database(notebook)
        if current_database is not None:
            _apply_use_database(
                connection,
                current_database,
                warnings,
                failed_use_databases,
            )

        total_cells = len(notebook.cells)
        for index, cell in enumerate(notebook.cells):
            remaining_count = total_cells - index - 1
            cell_number = index + 1
            if contains_transaction_statement(cell.sql):
                result = _empty_result(
                    CellStatus.REJECTED_TRANSACTION_STATEMENT,
                    "Transaction control statements are not allowed in notebook cells.",
                )
                cell_results.append(result)
                if stop_on_error:
                    break
                continue

            if cell.use_database is not None:
                _apply_use_database(
                    connection,
                    cell.use_database,
                    warnings,
                    failed_use_databases,
                )

            LOGGER.info(
                "cell_started",
                cell_index=cell_number,
                total_cells=total_cells,
            )
            cell_start_time = time.monotonic()
            result, error, cell_abandoned = _run_cell_in_thread(
                connection,
                cell.sql,
                max_rows,
                cell_timeout,
                interrupt_grace,
            )
            duration_seconds = round(time.monotonic() - cell_start_time, 1)
            if cell_abandoned:
                abandoned = True
                cell_results.append(
                    _empty_result(
                        CellStatus.TIMEOUT,
                        "Cell execution exceeded the timeout and could not "
                        "be interrupted.",
                    ),
                )
                LOGGER.info(
                    "cell_finished",
                    cell_index=cell_number,
                    total_cells=total_cells,
                    status=CellStatus.TIMEOUT.value,
                    duration_seconds=duration_seconds,
                )
                if not stop_on_error:
                    _append_skipped_abort_results(
                        cell_results,
                        remaining_count,
                        "Execution was abandoned after an uninterruptible timeout.",
                    )
                break

            if error is not None:
                cell_results.append(
                    _empty_result(CellStatus.ERROR, str(error)),
                )
                LOGGER.info(
                    "cell_finished",
                    cell_index=cell_number,
                    total_cells=total_cells,
                    status=CellStatus.ERROR.value,
                    duration_seconds=duration_seconds,
                )
                if stop_on_error:
                    break
                if _transaction_is_aborted(connection):
                    _append_skipped_abort_results(
                        cell_results,
                        remaining_count,
                        "Skipped because the transaction is aborted.",
                    )
                    if allow_writes:
                        commit_impossible = True
                    break
                continue

            if result is None:
                result = _empty_result(
                    CellStatus.ERROR,
                    "Cell execution ended without a result.",
                )
            cell_results.append(result)
            LOGGER.info(
                "cell_finished",
                cell_index=cell_number,
                total_cells=total_cells,
                status=result.status.value,
                duration_seconds=duration_seconds,
            )

            if result.status is not CellStatus.OK and stop_on_error:
                break
            if result.status is CellStatus.TIMEOUT and _transaction_is_aborted(
                connection,
            ):
                if allow_writes:
                    # Restarting the transaction here would let the final
                    # COMMIT persist only the writes made after the
                    # timeout, silently dropping everything before it. With
                    # --allow-writes the abort is terminal instead: skip the
                    # remaining cells and roll back everything at the end.
                    _append_skipped_abort_results(
                        cell_results,
                        remaining_count,
                        "Skipped because the transaction was aborted by a "
                        "timeout and --allow-writes was set.",
                    )
                    commit_impossible = True
                    break
                _restart_transaction(connection)
                _restore_default_database_if_invalid(
                    connection,
                    primary_database,
                    warnings,
                )

        if abandoned:
            warning = (
                "Execution was abandoned after an uninterruptible timeout; the "
                "database connection was intentionally left open (not "
                "committed, rolled back, or closed) because a worker thread "
                "may still be using it."
            )
            warnings.append(warning)
            LOGGER.warning(
                "connection_left_open_after_abandoned_timeout",
                warning=warning,
            )
            if allow_writes:
                writes_warning = (
                    "Nothing was committed: execution was abandoned after an "
                    "uninterruptible timeout before --allow-writes could "
                    "commit any changes."
                )
                warnings.append(writes_warning)
                LOGGER.warning(
                    "no_commit_after_abandoned_timeout",
                    warning=writes_warning,
                )
            return ExecutionReport(
                cell_results=cell_results,
                warnings=warnings,
                used_memory_fallback=used_memory_fallback,
                abandoned=True,
            )

        # The per-break-path commit_impossible bookkeeping above can miss
        # abort states reached through other exits from the cell loop (for
        # example stop_on_error breaking before the abort check), so the
        # final commit decision re-probes the transaction state. Committing
        # an aborted transaction either raises or silently degrades to a
        # rollback depending on the DuckDB version; both must surface as
        # the explicit no-commit warning instead. This probe never runs on
        # the abandoned path, which returned above without touching the
        # connection.
        if (
            allow_writes
            and not commit_impossible
            and _transaction_is_aborted(connection)
        ):
            commit_impossible = True

        if allow_writes and not commit_impossible:
            connection.execute("COMMIT")
        else:
            connection.execute("ROLLBACK")
            if allow_writes and commit_impossible:
                writes_warning = (
                    "No changes were committed: the transaction was aborted "
                    "before completion, so all writes were rolled back "
                    "despite --allow-writes."
                )
                warnings.append(writes_warning)
                LOGGER.warning(
                    "commit_skipped_after_aborted_transaction",
                    warning=writes_warning,
                )
    except _CellInterrupted as interrupted:
        LOGGER.warning(
            "execution_interrupted",
            worker_exited=interrupted.worker_exited,
        )
        if interrupted.worker_exited:
            # The worker thread returned (or was successfully interrupted
            # within interrupt_grace), so the connection is not being used
            # by another thread anymore: it is safe to roll back and close
            # it, mirroring the normal-error cleanup path below.
            try:
                connection.execute("ROLLBACK")
            except duckdb.Error:
                LOGGER.warning("rollback_after_interrupt_failed")
            connection.close()
        # else: the worker thread is still alive and may still be using the
        # connection. Mirroring the abandoned-timeout rationale above, the
        # connection is deliberately left untouched (no COMMIT/ROLLBACK,
        # no close()) because DuckDB serializes operations per connection
        # and touching it again from this thread could block forever.
        raise
    except Exception:
        try:
            connection.execute("ROLLBACK")
        except duckdb.Error:
            LOGGER.warning("rollback_after_executor_error_failed")
        connection.close()
        raise
    else:
        connection.close()

    return ExecutionReport(
        cell_results=cell_results,
        warnings=warnings,
        used_memory_fallback=used_memory_fallback,
        abandoned=False,
    )
