"""Unit tests for the DuckDB notebook executor layer.

Notes
-----
These tests intentionally describe the target behavior before the executor
implementation exists. They use real DuckDB databases and construct Notebook
models directly, so they do not depend on the blocked DuckDB UI JSON schema.
"""

from pathlib import Path

import duckdb
import pytest

from duckdb_ui_notebook_export.executor import (
    CellStatus,
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


def test_ut_x_021_notebook_connection_environment_is_replayed_before_execution(
    fresh_duckdb: Path,
) -> None:
    """UT-X-021: notebook connection environment is replayed before execution.

    Notes
    -----
    Blocked by design doc 6.2#1 because the DuckDB UI notebook JSON schema is
    unknown. Once unblocked, this should verify that ATTACH statements,
    extension loading, secrets, and variables from notebook JSON are reproduced
    as far as possible before executing cells.

    Traceability
    ------------
    design doc 4.2, ADR-008
    """
    pytest.skip("blocked by design doc 6.2#1: notebook JSON schema unknown")
