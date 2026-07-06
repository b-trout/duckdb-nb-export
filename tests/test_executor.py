"""Unit tests for the DuckDB notebook executor layer.

Notes
-----
These tests intentionally describe the target behavior before the executor
implementation exists. They use real DuckDB databases and construct Notebook
models directly, so they do not depend on the blocked DuckDB UI JSON schema.
"""

import contextlib
import threading
from pathlib import Path
from typing import cast

import duckdb
import pytest
import structlog.testing

from duckdb_ui_notebook_export.exceptions import TargetDatabaseError
from duckdb_ui_notebook_export.executor import (
    CellResult,
    CellStatus,
    _empty_result,
    _requires_existence_check,
    contains_transaction_statement,
    execute_notebook,
    resolve_target_db,
)
from duckdb_ui_notebook_export.models import Cell, Notebook


def make_notebook(
    *sql_cells: str,
    database_info: dict[str, object] | None = None,
    cell_types: list[str] | None = None,
) -> Notebook:
    """Build a Notebook model for executor tests.

    Parameters
    ----------
    sql_cells
        SQL text for each cell in order.
    database_info
        Optional notebook-level database metadata.
    cell_types
        Optional per-cell type names. Missing values default to ``"sql"``.

    Returns
    -------
    duckdb_ui_notebook_export.models.Notebook
        Notebook instance containing the requested cells.
    """
    resolved_cell_types = cell_types or ["sql"] * len(sql_cells)
    cells = [
        Cell(cell_type=cell_type, sql=sql)
        for cell_type, sql in zip(resolved_cell_types, sql_cells, strict=True)
    ]
    return Notebook(
        name="executor-unit-test",
        version_id="version-ut-x",
        cells=cells,
        database_info=database_info,
    )


def fetch_scalar(db_path: Path, sql: str) -> object:
    """Fetch one scalar value from a DuckDB database file.

    Parameters
    ----------
    db_path
        Path to the DuckDB database file.
    sql
        Query that returns at least one row and one column.

    Returns
    -------
    object
        First column of the first returned row.
    """
    with duckdb.connect(str(db_path)) as connection:
        row = connection.execute(sql).fetchone()
    assert row is not None
    return row[0]


def table_exists(db_path: Path, table_name: str) -> bool:
    """Check whether a table exists in a DuckDB database file.

    Parameters
    ----------
    db_path
        Path to the DuckDB database file.
    table_name
        Table name to look up in the default schema.

    Returns
    -------
    bool
        True when the table is present.
    """
    with duckdb.connect(str(db_path)) as connection:
        row = connection.execute(
            """
            SELECT count(*)
            FROM information_schema.tables
            WHERE table_schema = 'main'
              AND table_name = ?
            """,
            [table_name],
        ).fetchone()
    assert row is not None
    return row[0] == 1


def test_ut_x_001_begin_transaction_before_all_cells(fresh_duckdb: Path) -> None:
    """UT-X-001: execute_notebook starts one transaction before all cells.

    Notes
    -----
    DuckDB is not mocked, so BEGIN is verified through observable transaction
    behavior: an early write is visible to a later cell, but absent from a
    separate connection after the default rollback.
    """
    notebook = make_notebook(
        "CREATE TABLE tx_probe(id INTEGER);",
        "INSERT INTO tx_probe VALUES (1);",
        "SELECT count(*) FROM tx_probe;",
    )

    report = execute_notebook(notebook, str(fresh_duckdb))

    assert [result.status for result in report.cell_results] == [
        CellStatus.OK,
        CellStatus.OK,
        CellStatus.OK,
    ]
    assert report.cell_results[2].rows == [(1,)]
    assert not table_exists(fresh_duckdb, "tx_probe")


def test_ut_x_002_default_execution_rolls_back_changes(
    fresh_duckdb: Path,
) -> None:
    """UT-X-002: default execution rolls back all target database changes.

    Notes
    -----
    Persistence is checked through a separate DuckDB connection opened after
    execute_notebook returns.
    """
    notebook = make_notebook(
        "CREATE TABLE rolled_back(id INTEGER);",
        "INSERT INTO rolled_back VALUES (10);",
    )

    report = execute_notebook(notebook, str(fresh_duckdb))

    assert all(result.status is CellStatus.OK for result in report.cell_results)
    assert not table_exists(fresh_duckdb, "rolled_back")


def test_ut_x_003_allow_writes_commits_changes(fresh_duckdb: Path) -> None:
    """UT-X-003: allow_writes commits target database changes.

    Notes
    -----
    Persistence is checked through a separate DuckDB connection opened after
    execute_notebook returns.
    """
    notebook = make_notebook(
        "CREATE TABLE committed(id INTEGER);",
        "INSERT INTO committed VALUES (20);",
    )

    report = execute_notebook(notebook, str(fresh_duckdb), allow_writes=True)

    assert all(result.status is CellStatus.OK for result in report.cell_results)
    assert fetch_scalar(fresh_duckdb, "SELECT id FROM committed") == 20


def test_ut_x_004_catalog_exception_continues_to_later_cells(
    fresh_duckdb: Path,
) -> None:
    """UT-X-004: CatalogException cells fail without stopping later cells."""
    notebook = make_notebook(
        "SELECT * FROM missing_table;",
        "SELECT 42 AS answer;",
    )

    report = execute_notebook(notebook, str(fresh_duckdb))

    assert report.cell_results[0].status is CellStatus.ERROR
    assert "missing_table" in (report.cell_results[0].error_message or "")
    assert report.cell_results[1].status is CellStatus.OK
    assert report.cell_results[1].columns == ["answer"]
    assert report.cell_results[1].rows == [(42,)]


def test_ut_x_005_constraint_exception_skips_remaining_cells(
    fresh_duckdb: Path,
) -> None:
    """UT-X-005: ConstraintException aborts the transaction and skips later cells."""
    notebook = make_notebook(
        "CREATE TABLE unique_values(id INTEGER PRIMARY KEY);",
        "INSERT INTO unique_values VALUES (1);",
        "INSERT INTO unique_values VALUES (1);",
        "SELECT 99 AS should_not_run;",
    )

    report = execute_notebook(notebook, str(fresh_duckdb))

    assert [result.status for result in report.cell_results] == [
        CellStatus.OK,
        CellStatus.OK,
        CellStatus.ERROR,
        CellStatus.SKIPPED_ABORT,
    ]
    assert "constraint" in (report.cell_results[2].error_message or "").lower()
    assert "transaction" in (report.cell_results[3].error_message or "").lower()


def test_ut_x_006_abort_detection_uses_select_one_probe(
    fresh_duckdb: Path,
) -> None:
    """UT-X-006: abort state is detected immediately after a cell error.

    Notes
    -----
    The expected externally visible result of the SELECT 1 probe is that a
    ConstraintException marks the transaction aborted and the next cell is
    skipped instead of being attempted.
    """
    notebook = make_notebook(
        "CREATE TABLE abort_probe(id INTEGER PRIMARY KEY);",
        "INSERT INTO abort_probe VALUES (1);",
        "INSERT INTO abort_probe VALUES (1);",
        "SELECT 1 AS would_succeed_without_abort;",
    )

    report = execute_notebook(notebook, str(fresh_duckdb))

    assert report.cell_results[2].status is CellStatus.ERROR
    assert report.cell_results[3].status is CellStatus.SKIPPED_ABORT
    assert report.cell_results[3].rows == []


def test_ut_x_007_begin_statement_is_rejected(fresh_duckdb: Path) -> None:
    """UT-X-007: BEGIN statements are detected and rejected before execution."""
    assert contains_transaction_statement("BEGIN; SELECT 1;")

    notebook = make_notebook("BEGIN; CREATE TABLE rejected_begin(id INTEGER);")
    report = execute_notebook(notebook, str(fresh_duckdb))

    assert report.cell_results[0].status is CellStatus.REJECTED_TRANSACTION_STATEMENT
    assert "transaction" in (report.cell_results[0].error_message or "").lower()
    assert not table_exists(fresh_duckdb, "rejected_begin")


def test_ut_x_008_commit_and_rollback_statements_are_rejected(
    fresh_duckdb: Path,
) -> None:
    """UT-X-008: COMMIT and ROLLBACK statements are rejected before execution."""
    assert contains_transaction_statement("COMMIT;")
    assert contains_transaction_statement("ROLLBACK;")

    notebook = make_notebook(
        "CREATE TABLE rejected_commit(id INTEGER); COMMIT;",
        "CREATE TABLE rejected_rollback(id INTEGER); ROLLBACK;",
    )
    report = execute_notebook(notebook, str(fresh_duckdb))

    assert [result.status for result in report.cell_results] == [
        CellStatus.REJECTED_TRANSACTION_STATEMENT,
        CellStatus.REJECTED_TRANSACTION_STATEMENT,
    ]
    assert not table_exists(fresh_duckdb, "rejected_commit")
    assert not table_exists(fresh_duckdb, "rejected_rollback")


def test_ut_x_009_begin_literal_is_not_rejected(fresh_duckdb: Path) -> None:
    """UT-X-009: BEGIN inside a string literal is not a transaction statement."""
    assert not contains_transaction_statement("SELECT 'BEGIN' AS word;")

    notebook = make_notebook("SELECT 'BEGIN' AS word;")
    report = execute_notebook(notebook, str(fresh_duckdb))

    assert report.cell_results[0].status is CellStatus.OK
    assert report.cell_results[0].rows == [("BEGIN",)]


def test_ut_x_010_begin_comment_is_not_rejected(fresh_duckdb: Path) -> None:
    """UT-X-010: BEGIN inside a SQL comment is not a transaction statement."""
    assert not contains_transaction_statement("SELECT 1 AS value; -- BEGIN")

    notebook = make_notebook("SELECT 1 AS value; -- BEGIN")
    report = execute_notebook(notebook, str(fresh_duckdb))

    assert report.cell_results[0].status is CellStatus.OK
    assert report.cell_results[0].rows == [(1,)]


def test_ut_x_011_timeout_interrupts_long_running_cell(fresh_duckdb: Path) -> None:
    """UT-X-011: a cell exceeding cell_timeout is interrupted."""
    notebook = make_notebook(
        """
        SELECT sum(i * j)
        FROM range(100000000) AS lhs(i)
        CROSS JOIN range(100000000) AS rhs(j);
        """,
    )

    report = execute_notebook(
        notebook,
        str(fresh_duckdb),
        cell_timeout=0.5,
        interrupt_grace=1.0,
    )

    assert report.cell_results[0].status is CellStatus.TIMEOUT
    assert "timeout" in (report.cell_results[0].error_message or "").lower()


def test_ut_x_012_interrupt_within_grace_continues_later_cells(
    fresh_duckdb: Path,
) -> None:
    """UT-X-012: an interrupt that completes within grace allows later cells."""
    notebook = make_notebook(
        """
        SELECT sum(i * j)
        FROM range(100000000) AS lhs(i)
        CROSS JOIN range(100000000) AS rhs(j);
        """,
        "SELECT 42 AS after_timeout;",
    )

    report = execute_notebook(
        notebook,
        str(fresh_duckdb),
        cell_timeout=0.5,
        interrupt_grace=1.0,
    )

    assert report.cell_results[0].status is CellStatus.TIMEOUT
    assert report.cell_results[1].status is CellStatus.OK
    assert report.cell_results[1].rows == [(42,)]


def test_ut_x_013_uninterruptible_query_abandons_later_cells(
    fresh_duckdb: Path,
) -> None:
    """UT-X-013: an uninterruptible query abandons later cells after grace.

    Notes
    -----
    Real DuckDB queries are generally interruptible, and this failure mode
    cannot be reproduced reliably without mocking DuckDB internals.
    """
    pytest.skip("cannot reliably reproduce un-interruptible query with real DuckDB")


class _SpyConnection:
    """Spy wrapper recording SQL and lifecycle calls on a real connection.

    Parameters
    ----------
    real_connection
        Real DuckDB connection to delegate all operations to.

    Returns
    -------
    _SpyConnection
        Spy instance wrapping ``real_connection``.

    Raises
    ------
    None
        Construction does not raise package-specific exceptions.

    Notes
    -----
    Used by UT-X-025/UT-X-026 to observe that the executor never issues
    COMMIT/ROLLBACK or calls ``close()``/``interrupt()`` on the connection
    after an uninterruptible timeout is abandoned.
    """

    def __init__(self, real_connection: duckdb.DuckDBPyConnection) -> None:
        self._real_connection = real_connection
        self.executed_sql: list[str] = []
        self.close_called = False
        self.interrupt_called = False

    def execute(
        self,
        query: duckdb.Statement | str,
        parameters: object = None,
    ) -> duckdb.DuckDBPyConnection:
        """Record ``query`` and delegate execution to the real connection."""
        self.executed_sql.append(str(query))
        return self._real_connection.execute(query, parameters)

    def close(self) -> None:
        """Record that ``close`` was called without closing the real connection."""
        self.close_called = True

    def interrupt(self) -> None:
        """Record that ``interrupt`` was called and delegate it."""
        self.interrupt_called = True
        self._real_connection.interrupt()

    def __getattr__(self, name: str) -> object:
        """Delegate any other attribute access to the real connection."""
        return getattr(self._real_connection, name)


def test_ut_x_025_abandoned_timeout_never_touches_connection_again(
    fresh_duckdb: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT-X-025: an abandoned timeout must not COMMIT/ROLLBACK/close afterward.

    Notes
    -----
    Simulates the second cell of a 3-cell notebook returning
    ``abandoned=True`` from ``_run_cell_in_thread``. The executor must return
    promptly, mark ``report.abandoned`` True, produce
    ``[OK, TIMEOUT, SKIPPED_ABORT]`` results, and never issue a COMMIT or
    ROLLBACK nor call ``close()`` on the connection after abandonment,
    because the stuck daemon worker thread may still be using it.

    Traceability
    ------------
    Issue #28
    """
    import duckdb_ui_notebook_export.executor as executor_module

    real_connection = duckdb.connect(str(fresh_duckdb))
    spy = _SpyConnection(real_connection)
    monkeypatch.setattr(executor_module.duckdb, "connect", lambda *a, **k: spy)

    call_count = {"n": 0}
    original_run_cell_in_thread = executor_module._run_cell_in_thread

    def fake_run_cell_in_thread(
        connection: duckdb.DuckDBPyConnection,
        sql: str,
        max_rows: int,
        cell_timeout: float,
        interrupt_grace: float,
    ) -> tuple[CellResult | None, BaseException | None, bool]:
        call_count["n"] += 1
        if call_count["n"] == 2:
            return None, None, True
        return original_run_cell_in_thread(
            connection,
            sql,
            max_rows,
            cell_timeout,
            interrupt_grace,
        )

    monkeypatch.setattr(
        executor_module,
        "_run_cell_in_thread",
        fake_run_cell_in_thread,
    )

    notebook = make_notebook(
        "SELECT 1 AS first;",
        "SELECT 2 AS second;",
        "SELECT 3 AS third;",
    )

    report = execute_notebook(notebook, str(fresh_duckdb))

    assert [result.status for result in report.cell_results] == [
        CellStatus.OK,
        CellStatus.TIMEOUT,
        CellStatus.SKIPPED_ABORT,
    ]
    assert report.abandoned is True
    assert not any("ROLLBACK" in sql or "COMMIT" in sql for sql in spy.executed_sql)
    assert spy.close_called is False
    assert any("abandoned" in warning.lower() for warning in report.warnings)

    real_connection.close()


def test_ut_x_026_normal_run_reports_not_abandoned(fresh_duckdb: Path) -> None:
    """UT-X-026: a normal run reports ``abandoned`` as False.

    Traceability
    ------------
    Issue #28
    """
    notebook = make_notebook("SELECT 1 AS value;")

    report = execute_notebook(notebook, str(fresh_duckdb))

    assert report.abandoned is False


def test_ut_x_046_keyboard_interrupt_with_worker_exited_rolls_back_and_closes(
    fresh_duckdb: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT-X-046: KeyboardInterrupt with an exited worker rolls back and closes.

    Notes
    -----
    Simulates a Ctrl-C landing while ``_run_cell_in_thread`` is waiting on
    the worker thread's join: ``connection.interrupt()`` succeeded and the
    worker thread returned within ``interrupt_grace``, so it is safe for
    ``execute_notebook`` to issue a best-effort ``ROLLBACK`` and ``close()``
    the connection before re-raising ``KeyboardInterrupt``.

    Traceability
    ------------
    Issue #57
    """
    import duckdb_ui_notebook_export.executor as executor_module

    real_connection = duckdb.connect(str(fresh_duckdb))
    spy = _SpyConnection(real_connection)
    monkeypatch.setattr(executor_module.duckdb, "connect", lambda *a, **k: spy)

    def fake_run_cell_in_thread(
        connection: duckdb.DuckDBPyConnection,
        sql: str,
        max_rows: int,
        cell_timeout: float,
        interrupt_grace: float,
    ) -> tuple[CellResult | None, BaseException | None, bool]:
        del connection, sql, max_rows, cell_timeout, interrupt_grace
        raise executor_module._CellInterrupted(worker_exited=True)

    monkeypatch.setattr(
        executor_module,
        "_run_cell_in_thread",
        fake_run_cell_in_thread,
    )

    notebook = make_notebook("SELECT 1 AS value;")

    with pytest.raises(KeyboardInterrupt):
        execute_notebook(notebook, str(fresh_duckdb))

    assert any("ROLLBACK" in sql for sql in spy.executed_sql)
    assert spy.close_called is True

    real_connection.close()


def test_ut_x_047_keyboard_interrupt_with_worker_alive_leaves_connection_untouched(
    fresh_duckdb: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT-X-047: KeyboardInterrupt with a live worker never touches the connection.

    Notes
    -----
    Simulates the uninterruptible case: the worker thread is still alive
    after ``interrupt_grace``, so ``execute_notebook`` must not issue
    COMMIT/ROLLBACK or ``close()`` on the connection (mirroring the
    abandoned-timeout rationale) before re-raising ``KeyboardInterrupt``.

    Traceability
    ------------
    Issue #57
    """
    import duckdb_ui_notebook_export.executor as executor_module

    real_connection = duckdb.connect(str(fresh_duckdb))
    spy = _SpyConnection(real_connection)
    monkeypatch.setattr(executor_module.duckdb, "connect", lambda *a, **k: spy)

    def fake_run_cell_in_thread(
        connection: duckdb.DuckDBPyConnection,
        sql: str,
        max_rows: int,
        cell_timeout: float,
        interrupt_grace: float,
    ) -> tuple[CellResult | None, BaseException | None, bool]:
        del connection, sql, max_rows, cell_timeout, interrupt_grace
        raise executor_module._CellInterrupted(worker_exited=False)

    monkeypatch.setattr(
        executor_module,
        "_run_cell_in_thread",
        fake_run_cell_in_thread,
    )

    notebook = make_notebook("SELECT 1 AS value;")

    with pytest.raises(KeyboardInterrupt):
        execute_notebook(notebook, str(fresh_duckdb))

    assert not any("ROLLBACK" in sql or "COMMIT" in sql for sql in spy.executed_sql)
    assert spy.close_called is False

    real_connection.close()


def test_ut_x_048_run_cell_in_thread_raises_cell_interrupted_on_join_interrupt(
    fresh_duckdb: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT-X-048: a KeyboardInterrupt during the initial join is handled.

    Notes
    -----
    Directly exercises ``_run_cell_in_thread``: patches ``threading.Thread``
    so the first ``join`` call raises ``KeyboardInterrupt`` (simulating a
    signal arriving while the main thread waits), then asserts that
    ``connection.interrupt()`` is called and ``_CellInterrupted`` is raised
    with ``worker_exited`` reflecting whether the thread was still alive
    after the grace-period join.

    Traceability
    ------------
    Issue #57
    """
    import duckdb_ui_notebook_export.executor as executor_module

    real_connection = duckdb.connect(str(fresh_duckdb))
    spy = _SpyConnection(real_connection)

    class _RaisingOnFirstJoinThread(threading.Thread):
        _join_calls = 0

        def join(self, timeout: float | None = None) -> None:
            type(self)._join_calls += 1
            if type(self)._join_calls == 1:
                raise KeyboardInterrupt
            super().join(timeout)

    monkeypatch.setattr(executor_module.threading, "Thread", _RaisingOnFirstJoinThread)

    with pytest.raises(executor_module._CellInterrupted) as exc_info:
        executor_module._run_cell_in_thread(
            cast("duckdb.DuckDBPyConnection", spy),
            "SELECT 1;",
            1000,
            300.0,
            5.0,
        )

    assert spy.interrupt_called is True
    assert exc_info.value.worker_exited is True

    real_connection.close()


def test_ut_x_014_cli_db_overrides_notebook_database_info(
    tmp_path: Path,
) -> None:
    """UT-X-014: cli_db takes priority over notebook database metadata."""
    notebook_db = tmp_path / "notebook.duckdb"
    cli_db = tmp_path / "cli.duckdb"
    notebook = make_notebook(database_info={"path": str(notebook_db)})

    resolved_db, used_memory_fallback = resolve_target_db(notebook, str(cli_db))

    assert resolved_db == str(cli_db)
    assert used_memory_fallback is False


def test_ut_x_015_notebook_database_info_resolves_target_db(
    tmp_path: Path,
) -> None:
    """UT-X-015: notebook database metadata resolves the target database."""
    notebook_db = tmp_path / "notebook.duckdb"
    notebook = make_notebook(database_info={"path": str(notebook_db)})

    resolved_db, used_memory_fallback = resolve_target_db(notebook, None)

    assert resolved_db == str(notebook_db)
    assert used_memory_fallback is False


def test_ut_x_016_unresolved_db_falls_back_to_memory_with_warning() -> None:
    """UT-X-016: unresolved target database falls back to :memory: with warning."""
    notebook = make_notebook("SELECT 1 AS value;")

    resolved_db, used_memory_fallback = resolve_target_db(notebook, None)
    report = execute_notebook(notebook, resolved_db)

    assert resolved_db == ":memory:"
    assert used_memory_fallback is True
    assert report.used_memory_fallback is True
    assert any(":memory:" in warning for warning in report.warnings)
    assert report.cell_results[0].status is CellStatus.OK


def test_ut_x_041_explicit_memory_db_with_fallback_false_has_no_warning() -> None:
    """UT-X-041: explicit ``--db :memory:`` must not warn about fallback.

    Notes
    -----
    ``used_memory_fallback=False`` models the CLI path where
    ``resolve_target_db`` returned ``False`` because ``--db`` was given
    explicitly as ``:memory:``, even though the resulting database string is
    still the literal ``":memory:"``.

    Traceability
    ------------
    Issue #49
    """
    notebook = make_notebook("SELECT 1 AS value;")

    report = execute_notebook(
        notebook,
        ":memory:",
        used_memory_fallback=False,
    )

    assert report.used_memory_fallback is False
    assert not any(
        "no target database was resolved" in warning.lower()
        for warning in report.warnings
    )
    assert report.cell_results[0].status is CellStatus.OK


def test_ut_x_042_used_memory_fallback_true_forces_warning() -> None:
    """UT-X-042: an explicit fallback flag of True still warns.

    Traceability
    ------------
    Issue #49
    """
    notebook = make_notebook("SELECT 1 AS value;")

    report = execute_notebook(
        notebook,
        ":memory:",
        used_memory_fallback=True,
    )

    assert report.used_memory_fallback is True
    assert any(
        "no target database was resolved" in warning.lower()
        for warning in report.warnings
    )


def test_ut_x_043_used_memory_fallback_none_keeps_legacy_recompute() -> None:
    """UT-X-043: omitting the flag preserves the legacy recompute-from-string.

    Notes
    -----
    Direct callers that do not pass ``used_memory_fallback`` (or pass
    ``None`` explicitly) must keep the pre-#49 behavior of inferring the
    flag from ``db == ":memory:"``, for backward compatibility.

    Traceability
    ------------
    Issue #49
    """
    notebook = make_notebook("SELECT 1 AS value;")

    report = execute_notebook(notebook, ":memory:")

    assert report.used_memory_fallback is True
    assert any(
        "no target database was resolved" in warning.lower()
        for warning in report.warnings
    )


def test_ut_x_044_cell_started_and_finished_events_are_logged(
    fresh_duckdb: Path,
) -> None:
    """UT-X-044: each cell logs ``cell_started``/``cell_finished`` at INFO.

    Notes
    -----
    Covers a 2-cell notebook where the second cell errors, asserting that
    both cells produce matching ``cell_started``/``cell_finished`` pairs
    with correct 1-based indices, total cell count, and final status
    (including the ``ERROR`` cell).

    Traceability
    ------------
    Issue #51
    """
    notebook = make_notebook(
        "SELECT 1 AS first;",
        "SELECT * FROM does_not_exist;",
    )

    with structlog.testing.capture_logs() as logs:
        report = execute_notebook(notebook, str(fresh_duckdb))

    assert report.cell_results[0].status is CellStatus.OK
    assert report.cell_results[1].status is CellStatus.ERROR

    started_events = [entry for entry in logs if entry["event"] == "cell_started"]
    finished_events = [entry for entry in logs if entry["event"] == "cell_finished"]

    assert [entry["cell_index"] for entry in started_events] == [1, 2]
    assert all(entry["total_cells"] == 2 for entry in started_events)
    assert all(entry["log_level"] == "info" for entry in started_events)

    assert [entry["cell_index"] for entry in finished_events] == [1, 2]
    assert all(entry["total_cells"] == 2 for entry in finished_events)
    assert all(entry["log_level"] == "info" for entry in finished_events)
    assert [entry["status"] for entry in finished_events] == ["OK", "ERROR"]
    assert all(
        isinstance(entry["duration_seconds"], float) for entry in finished_events
    )


def test_ut_x_045_cell_finished_logged_for_timeout(fresh_duckdb: Path) -> None:
    """UT-X-045: a timed-out cell still logs ``cell_finished`` with TIMEOUT.

    Traceability
    ------------
    Issue #51
    """
    notebook = make_notebook(
        """
        SELECT sum(i * j)
        FROM range(100000000) AS lhs(i)
        CROSS JOIN range(100000000) AS rhs(j);
        """,
    )

    with structlog.testing.capture_logs() as logs:
        report = execute_notebook(
            notebook,
            str(fresh_duckdb),
            cell_timeout=0.5,
            interrupt_grace=1.0,
        )

    assert report.cell_results[0].status is CellStatus.TIMEOUT
    finished_events = [entry for entry in logs if entry["event"] == "cell_finished"]
    assert len(finished_events) == 1
    assert finished_events[0]["status"] == "TIMEOUT"
    assert finished_events[0]["cell_index"] == 1
    assert finished_events[0]["total_cells"] == 1


def test_ut_x_017_fetches_only_max_rows_plus_one(fresh_duckdb: Path) -> None:
    """UT-X-017: result fetching uses max_rows plus one for truncation."""
    notebook = make_notebook("SELECT i FROM range(6) AS generated(i) ORDER BY i;")

    report = execute_notebook(notebook, str(fresh_duckdb), max_rows=5)

    assert report.cell_results[0].status is CellStatus.OK
    assert report.cell_results[0].rows == [(0,), (1,), (2,), (3,), (4,)]
    assert report.cell_results[0].truncated is True


def test_ut_x_018_multi_statement_cell_displays_last_result_only(
    fresh_duckdb: Path,
) -> None:
    """UT-X-018: multi-statement cells expose only the last result set."""
    notebook = make_notebook(
        """
        CREATE TABLE multi_statement(id INTEGER);
        INSERT INTO multi_statement VALUES (1), (2);
        SELECT count(*) AS row_count FROM multi_statement;
        """,
    )

    report = execute_notebook(notebook, str(fresh_duckdb))

    assert report.cell_results[0].status is CellStatus.OK
    assert report.cell_results[0].columns == ["row_count"]
    assert report.cell_results[0].rows == [(2,)]


def test_ut_x_019_stop_on_error_aborts_after_first_failure(
    fresh_duckdb: Path,
) -> None:
    """UT-X-019: stop_on_error stops execution after the first failed cell."""
    notebook = make_notebook(
        "SELECT * FROM missing_for_stop_on_error;",
        "CREATE TABLE should_not_run(id INTEGER);",
    )

    report = execute_notebook(notebook, str(fresh_duckdb), stop_on_error=True)

    assert len(report.cell_results) == 1
    assert report.cell_results[0].status is CellStatus.ERROR
    assert not table_exists(fresh_duckdb, "should_not_run")


def test_ut_x_020_no_external_access_disables_file_export(
    fresh_duckdb: Path,
    tmp_path: Path,
) -> None:
    """UT-X-020: no_external_access disables external file operations."""
    output_csv = tmp_path / "blocked.csv"
    notebook = make_notebook(
        f"COPY (SELECT 1 AS value) TO '{output_csv.as_posix()}' (FORMAT CSV);",
    )

    report = execute_notebook(
        notebook,
        str(fresh_duckdb),
        no_external_access=True,
    )

    assert report.cell_results[0].status is CellStatus.ERROR
    assert "external" in (report.cell_results[0].error_message or "").lower()
    assert not output_csv.exists()


def test_ut_x_021_notebook_database_names_are_replayed_with_use(
    fresh_duckdb: Path,
    tmp_path: Path,
) -> None:
    """UT-X-021: stored database names are replayed via best-effort USE.

    Notes
    -----
    Stored format v3 records database names only (design doc 6.3#9), so
    environment replay is limited to ``USE``: the notebook-level
    ``currentDatabase`` is applied once after BEGIN, and a cell-level
    ``useDatabase`` is applied before that cell. The cell-level name here
    resolves because an earlier cell ATTACHes it inside the transaction
    (AT-008 guards that ATTACH works in a transaction).

    Traceability
    ------------
    design doc 4.2, ADR-008
    """
    other_db = tmp_path / "replay-other.duckdb"
    duckdb.connect(str(other_db)).close()

    notebook = Notebook(
        name="executor-unit-test",
        version_id="version-ut-x",
        cells=[
            Cell(sql=f"ATTACH '{other_db.as_posix()}' AS replay_other"),
            Cell(sql="SELECT current_database()", use_database="replay_other"),
        ],
        database_info={"current_database": fresh_duckdb.stem},
    )

    report = execute_notebook(notebook, str(fresh_duckdb))

    statuses = [result.status for result in report.cell_results]
    assert statuses == [CellStatus.OK, CellStatus.OK]
    assert report.cell_results[1].rows == [("replay_other",)]
    assert report.warnings == []


def test_ut_x_022_unresolvable_database_name_warns_once_and_continues(
    fresh_duckdb: Path,
) -> None:
    """UT-X-022: an unresolvable stored database name warns and continues.

    Notes
    -----
    A failed ``USE`` raises ``CatalogException`` without aborting the
    transaction, so cells still run against the current database. Each
    unresolvable name produces exactly one warning even when repeated
    across cells.

    Traceability
    ------------
    design doc 4.2, ADR-008
    """
    notebook = Notebook(
        name="executor-unit-test",
        version_id="version-ut-x",
        cells=[
            Cell(sql="SELECT 1", use_database="missing_db"),
            Cell(sql="SELECT 2", use_database="missing_db"),
        ],
        database_info={"current_database": "missing_notebook_db"},
    )

    report = execute_notebook(notebook, str(fresh_duckdb))

    statuses = [result.status for result in report.cell_results]
    assert statuses == [CellStatus.OK, CellStatus.OK]
    assert report.cell_results[0].rows == [(1,)]
    assert report.cell_results[1].rows == [(2,)]

    use_warnings = [w for w in report.warnings if "Could not switch" in w]
    assert len(use_warnings) == 2
    assert any("missing_notebook_db" in w for w in use_warnings)
    assert any("missing_db" in w for w in use_warnings)


def test_ut_x_023_timeout_rollback_restores_valid_default_database(
    fresh_duckdb: Path,
    tmp_path: Path,
) -> None:
    """UT-X-023: timeout-rollback recovery restores a valid default database.

    Notes
    -----
    A transaction-scoped ``ATTACH`` is undone by the ``ROLLBACK`` that
    ``_restart_transaction`` issues after an aborted timeout, but the
    connection's default catalog can still point at the now-detached
    database. Left unprobed, every later cell that does not set
    ``use_database`` would run (or fail) against a catalog that no longer
    exists. The executor must detect this and best-effort restore the
    primary catalog with a warning.

    Traceability
    ------------
    design doc 4.2, ADR-007, ADR-008
    """
    other_db = tmp_path / "restore-other.duckdb"
    duckdb.connect(str(other_db)).close()
    primary_name = fresh_duckdb.stem

    notebook = Notebook(
        name="executor-unit-test",
        version_id="version-ut-x",
        cells=[
            Cell(sql=f"ATTACH '{other_db.as_posix()}' AS other"),
            Cell(sql="SELECT 1", use_database="other"),
            Cell(
                sql="""
                CREATE TABLE big AS
                SELECT lhs.i
                FROM range(100000000) lhs(i)
                CROSS JOIN range(1000) rhs(j);
                """,
            ),
            Cell(sql="SELECT current_database()"),
        ],
    )

    report = execute_notebook(
        notebook,
        str(fresh_duckdb),
        cell_timeout=0.5,
        interrupt_grace=5.0,
    )

    last_result = report.cell_results[-1]
    assert last_result.status is CellStatus.OK
    assert last_result.rows == [(primary_name,)]
    assert any(
        "reset" in warning.lower() and primary_name in warning
        for warning in report.warnings
    )


def test_ut_x_024_use_database_is_retried_after_earlier_failure(
    fresh_duckdb: Path,
    tmp_path: Path,
) -> None:
    """UT-X-024: a USE that failed once is retried for a later cell.

    Notes
    -----
    ``failed_databases`` exists to deduplicate warnings for names that
    remain unresolvable, not to permanently block retries: an earlier
    cell can ``ATTACH`` a database whose name previously failed at the
    notebook level (nothing was attached yet at ``BEGIN``). The executor
    must still attempt ``USE`` for that name on a later cell.

    Traceability
    ------------
    design doc 4.2, ADR-008
    """
    other_db = tmp_path / "retry-other.duckdb"
    duckdb.connect(str(other_db)).close()

    notebook = Notebook(
        name="executor-unit-test",
        version_id="version-ut-x",
        cells=[
            Cell(sql=f"ATTACH '{other_db.as_posix()}' AS replay_other"),
            Cell(sql="SELECT current_database()", use_database="replay_other"),
        ],
        database_info={"current_database": "replay_other"},
    )

    report = execute_notebook(notebook, str(fresh_duckdb))

    statuses = [result.status for result in report.cell_results]
    assert statuses == [CellStatus.OK, CellStatus.OK]
    assert report.cell_results[1].rows == [("replay_other",)]

    use_warnings = [w for w in report.warnings if "Could not switch" in w]
    assert len(use_warnings) == 1


def test_ut_x_027_requires_existence_check_for_plain_local_paths() -> None:
    """UT-X-027: plain local paths require an existence check.

    Traceability
    ------------
    Issue #30
    """
    assert _requires_existence_check("relative/path.duckdb") is True
    assert _requires_existence_check("/absolute/path.duckdb") is True


def test_ut_x_028_requires_existence_check_windows_drive_letter() -> None:
    """UT-X-028: a Windows drive letter is treated as a local path, not a URI.

    Notes
    -----
    A single-character scheme (``C:``) must not match the URI-scheme skip
    rule, which requires a 2+ character scheme per RFC 3986.

    Traceability
    ------------
    Issue #30
    """
    assert _requires_existence_check("C:\\data\\x.db") is True


def test_ut_x_029_requires_existence_check_skips_memory() -> None:
    """UT-X-029: ``:memory:`` never requires an existence check.

    Traceability
    ------------
    Issue #30
    """
    assert _requires_existence_check(":memory:") is False


def test_ut_x_030_requires_existence_check_skips_uri_schemes() -> None:
    """UT-X-030: multi-character URI-style schemes skip the existence check.

    Notes
    -----
    Preserves ``md:``/``s3:``-style DuckDB connect strings.

    Traceability
    ------------
    Issue #30
    """
    assert _requires_existence_check("md:my_database") is False
    assert _requires_existence_check("s3://bucket/key.duckdb") is False
    assert _requires_existence_check("someschemey:whatever") is False


def test_ut_x_031_nonexistent_db_path_raises_and_creates_no_file(
    tmp_path: Path,
) -> None:
    """UT-X-031: a mistyped ``--db`` path raises without creating a file.

    Traceability
    ------------
    Issue #30
    """
    missing_db = tmp_path / "typo.duckdb"
    notebook = make_notebook("SELECT 1;")

    with pytest.raises(TargetDatabaseError):
        execute_notebook(notebook, str(missing_db))

    assert not missing_db.exists()


def test_ut_x_032_existing_db_path_still_works(fresh_duckdb: Path) -> None:
    """UT-X-032: an existing ``--db`` file path executes normally.

    Traceability
    ------------
    Issue #30
    """
    notebook = make_notebook("SELECT 1 AS value;")

    report = execute_notebook(notebook, str(fresh_duckdb))

    assert report.cell_results[0].status is CellStatus.OK


def test_ut_x_033_memory_target_is_unaffected_by_existence_check() -> None:
    """UT-X-033: ``:memory:`` is unaffected by the existence check.

    Traceability
    ------------
    Issue #30
    """
    notebook = make_notebook("SELECT 1 AS value;")

    report = execute_notebook(notebook, ":memory:")

    assert report.cell_results[0].status is CellStatus.OK


def test_ut_x_034_read_only_write_cell_fails_but_export_completes(
    fresh_duckdb: Path,
) -> None:
    """UT-X-034: read_only rejects writes but the export still completes.

    Notes
    -----
    Unlike an ordinary CatalogException, DuckDB aborts the transaction when
    a write is attempted on a read-only connection, so the remaining cell
    is skipped rather than executed -- matching the existing abort-handling
    path (UT-X-005/UT-X-006) rather than UT-X-004's continue-after-error
    path.

    Traceability
    ------------
    Issue #31
    """
    notebook = make_notebook(
        "CREATE TABLE should_fail_read_only(id INTEGER);",
        "SELECT 1 AS should_be_skipped;",
    )

    report = execute_notebook(notebook, str(fresh_duckdb), read_only=True)

    assert report.cell_results[0].status is CellStatus.ERROR
    assert report.cell_results[1].status is CellStatus.SKIPPED_ABORT
    assert not table_exists(fresh_duckdb, "should_fail_read_only")


def test_ut_x_035_read_only_with_memory_target_raises(fresh_duckdb: Path) -> None:
    """UT-X-035: read_only with a ``:memory:`` target raises.

    Notes
    -----
    ``duckdb.connect(":memory:", read_only=True)`` is invalid in DuckDB;
    the executor must reject this combination with a clear message before
    attempting to connect.

    Traceability
    ------------
    Issue #31
    """
    del fresh_duckdb
    notebook = make_notebook("SELECT 1;")

    with pytest.raises(TargetDatabaseError):
        execute_notebook(notebook, ":memory:", read_only=True)


def test_ut_x_036_read_only_with_nonexistent_path_raises_target_database_error(
    tmp_path: Path,
) -> None:
    """UT-X-036: read_only with a missing file raises before connecting.

    Notes
    -----
    Combines with the issue #30 existence check: a nonexistent path plus
    ``read_only=True`` must raise ``TargetDatabaseError`` rather than
    letting DuckDB attempt (and fail) to open a read-only connection to a
    file that does not exist.

    Traceability
    ------------
    Issue #31
    """
    missing_db = tmp_path / "missing-read-only.duckdb"
    notebook = make_notebook("SELECT 1;")

    with pytest.raises(TargetDatabaseError):
        execute_notebook(notebook, str(missing_db), read_only=True)

    assert not missing_db.exists()


def test_ut_x_037_allow_writes_error_abort_never_partial_commits(
    fresh_duckdb: Path,
) -> None:
    """UT-X-037: allow_writes never partial-commits after an error-abort.

    Notes
    -----
    Before this fix, an error that aborted the transaction still hit a
    final ``COMMIT`` attempt on an aborted transaction, which raises and
    propagates out of ``execute_notebook`` with no HTML written at all.
    Now the export completes normally: remaining cells are marked
    ``SKIPPED_ABORT``, a prominent warning is recorded, and the final
    transaction control is ``ROLLBACK`` instead of ``COMMIT`` so earlier
    writes in the same transaction are not silently persisted either.

    Traceability
    ------------
    Issue #32
    """
    notebook = make_notebook(
        "CREATE TABLE allow_writes_abort(id INTEGER PRIMARY KEY);",
        "INSERT INTO allow_writes_abort VALUES (1);",
        "INSERT INTO allow_writes_abort VALUES (1);",
        "SELECT 99 AS should_not_run;",
    )

    report = execute_notebook(notebook, str(fresh_duckdb), allow_writes=True)

    assert [result.status for result in report.cell_results] == [
        CellStatus.OK,
        CellStatus.OK,
        CellStatus.ERROR,
        CellStatus.SKIPPED_ABORT,
    ]
    assert any(
        "no changes were committed" in warning.lower() for warning in report.warnings
    )
    assert not table_exists(fresh_duckdb, "allow_writes_abort")


def test_ut_x_041_stop_on_error_allow_writes_abort_never_commits(
    fresh_duckdb: Path,
) -> None:
    """UT-X-041: stop_on_error + allow_writes never COMMITs an aborted txn.

    Notes
    -----
    With ``stop_on_error=True`` the error branch breaks out of the cell
    loop before the ``_transaction_is_aborted`` check that sets
    ``commit_impossible``, so the final handling used to reach
    ``COMMIT`` on an aborted transaction. Depending on the DuckDB
    version that either raises (propagating out of ``execute_notebook``
    with no HTML at all) or silently degrades to a rollback with no
    warning that nothing was committed. The final commit decision must
    therefore re-probe the transaction state for every break path:
    ``execute_notebook`` must return a normal report, roll back, and
    carry the "No changes were committed" warning.

    Traceability
    ------------
    Issue #32
    """
    notebook = make_notebook(
        "CREATE TABLE stop_abort(id INTEGER PRIMARY KEY);"
        "INSERT INTO stop_abort VALUES (1);",
        "INSERT INTO stop_abort VALUES (1);",
        "SELECT 99 AS should_not_run;",
    )

    report = execute_notebook(
        notebook,
        str(fresh_duckdb),
        allow_writes=True,
        stop_on_error=True,
    )

    assert [result.status for result in report.cell_results] == [
        CellStatus.OK,
        CellStatus.ERROR,
    ]
    assert any(
        "no changes were committed" in warning.lower() for warning in report.warnings
    )
    assert not table_exists(fresh_duckdb, "stop_abort")


def test_ut_x_038_allow_writes_timeout_abort_is_terminal_not_restarted(
    fresh_duckdb: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT-X-038: allow_writes treats a timeout-abort as terminal.

    Notes
    -----
    Simulates a TIMEOUT result whose underlying transaction is actually
    aborted (a real failing statement is executed through the fake to
    leave the transaction in an aborted state, deterministically, with no
    sleep-based timing). With ``allow_writes=True`` the executor must NOT
    call ``_restart_transaction`` (which would silently commit only
    post-timeout writes); it must instead skip all remaining cells and
    finish with ``ROLLBACK``.

    Traceability
    ------------
    Issue #32
    """
    import duckdb_ui_notebook_export.executor as executor_module

    original_run_cell_in_thread = executor_module._run_cell_in_thread
    call_count = {"n": 0}

    def fake_run_cell_in_thread(
        connection: duckdb.DuckDBPyConnection,
        sql: str,
        max_rows: int,
        cell_timeout: float,
        interrupt_grace: float,
    ) -> tuple[CellResult | None, BaseException | None, bool]:
        call_count["n"] += 1
        if call_count["n"] != 2:
            return original_run_cell_in_thread(
                connection,
                sql,
                max_rows,
                cell_timeout,
                interrupt_grace,
            )
        # Actually abort the transaction (a ConstraintException, unlike a
        # CatalogException, leaves the transaction aborted), then report a
        # TIMEOUT result, to deterministically reproduce "cell timed out and
        # the transaction ended up aborted" without any real sleep-based
        # timing.
        with contextlib.suppress(duckdb.Error):
            connection.execute(
                "INSERT INTO allow_writes_timeout VALUES (1), (1)",
            )
        return (
            _empty_result(
                CellStatus.TIMEOUT,
                "Cell execution exceeded the timeout and was interrupted.",
            ),
            None,
            False,
        )

    monkeypatch.setattr(
        executor_module,
        "_run_cell_in_thread",
        fake_run_cell_in_thread,
    )

    notebook = make_notebook(
        "CREATE TABLE allow_writes_timeout(id INTEGER PRIMARY KEY);",
        "SELECT 1 AS times_out;",
        "SELECT 2 AS should_be_skipped;",
    )

    report = execute_notebook(notebook, str(fresh_duckdb), allow_writes=True)

    assert [result.status for result in report.cell_results] == [
        CellStatus.OK,
        CellStatus.TIMEOUT,
        CellStatus.SKIPPED_ABORT,
    ]
    assert "timeout" in (report.cell_results[2].error_message or "").lower()
    assert any(
        "no changes were committed" in warning.lower() for warning in report.warnings
    )
    assert not table_exists(fresh_duckdb, "allow_writes_timeout")


def test_ut_x_039_allow_writes_false_timeout_abort_still_restarts(
    fresh_duckdb: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT-X-039: allow_writes=False keeps the restart-and-continue behavior.

    Notes
    -----
    Guards that the issue #32 fix is scoped to ``allow_writes=True``: the
    default (rollback) mode must keep restarting the transaction after a
    timeout-abort so later cells still run, matching the pre-existing
    UT-X-023 behavior.

    Traceability
    ------------
    Issue #32
    """
    import duckdb_ui_notebook_export.executor as executor_module

    original_run_cell_in_thread = executor_module._run_cell_in_thread
    call_count = {"n": 0}

    def fake_run_cell_in_thread(
        connection: duckdb.DuckDBPyConnection,
        sql: str,
        max_rows: int,
        cell_timeout: float,
        interrupt_grace: float,
    ) -> tuple[CellResult | None, BaseException | None, bool]:
        call_count["n"] += 1
        if call_count["n"] != 2:
            return original_run_cell_in_thread(
                connection,
                sql,
                max_rows,
                cell_timeout,
                interrupt_grace,
            )
        with contextlib.suppress(duckdb.Error):
            connection.execute(
                "INSERT INTO default_mode_timeout VALUES (1), (1)",
            )
        return (
            _empty_result(
                CellStatus.TIMEOUT,
                "Cell execution exceeded the timeout and was interrupted.",
            ),
            None,
            False,
        )

    monkeypatch.setattr(
        executor_module,
        "_run_cell_in_thread",
        fake_run_cell_in_thread,
    )

    notebook = make_notebook(
        "CREATE TABLE default_mode_timeout(id INTEGER PRIMARY KEY);",
        "SELECT 1 AS times_out;",
        "SELECT 2 AS continues_after_restart;",
    )

    report = execute_notebook(notebook, str(fresh_duckdb))

    assert [result.status for result in report.cell_results] == [
        CellStatus.OK,
        CellStatus.TIMEOUT,
        CellStatus.OK,
    ]
    assert report.cell_results[2].rows == [(2,)]


def test_ut_x_040_abandoned_with_allow_writes_warns_nothing_committed(
    fresh_duckdb: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT-X-040: an abandoned run with allow_writes warns nothing committed.

    Notes
    -----
    The issue #28 abandoned path takes precedence and never touches the
    connection; with ``allow_writes=True`` the report must still make
    clear to the user that nothing was committed.

    Traceability
    ------------
    Issue #32
    """
    import duckdb_ui_notebook_export.executor as executor_module

    real_connection = duckdb.connect(str(fresh_duckdb))
    monkeypatch.setattr(
        executor_module.duckdb,
        "connect",
        lambda *a, **k: real_connection,
    )

    def fake_run_cell_in_thread(
        connection: duckdb.DuckDBPyConnection,
        sql: str,
        max_rows: int,
        cell_timeout: float,
        interrupt_grace: float,
    ) -> tuple[CellResult | None, BaseException | None, bool]:
        del connection, sql, max_rows, cell_timeout, interrupt_grace
        return None, None, True

    monkeypatch.setattr(
        executor_module,
        "_run_cell_in_thread",
        fake_run_cell_in_thread,
    )

    notebook = make_notebook("SELECT 1 AS value;")

    report = execute_notebook(notebook, str(fresh_duckdb), allow_writes=True)

    assert report.abandoned is True
    assert any(
        "nothing" in warning.lower() and "committed" in warning.lower()
        for warning in report.warnings
    )

    real_connection.close()
