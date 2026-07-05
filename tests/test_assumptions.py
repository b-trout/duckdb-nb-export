"""Assumption tests for DuckDB and DuckDB UI notebook storage.

Parameters
----------
None
    This module does not accept parameters.

Returns
-------
None
    Importing this module registers pytest tests.

Raises
------
None
    Importing this module should not raise package-specific exceptions.

Notes
-----
AT-011 is reserved by the test design document and is intentionally not
implemented here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import duckdb
import pytest

from duckdb_ui_notebook_export.models import StoredNotebook

pytestmark = pytest.mark.assumptions


def _connect_result(db_path: Path, *, read_only: bool) -> dict[str, str]:
    """Attempt to open a DuckDB database and return a structured result.

    Parameters
    ----------
    db_path
        Path to the DuckDB database file.
    read_only
        Whether to request a read-only connection.

    Returns
    -------
    dict[str, str]
        Result payload containing status and error details.

    Raises
    ------
    None
        Connection failures are captured in the returned payload.
    """
    try:
        duckdb.connect(str(db_path), read_only=read_only).close()
    except Exception as exc:
        return {
            "status": "failed",
            "type": type(exc).__name__,
            "message": str(exc),
        }
    return {"status": "ok", "type": "", "message": ""}


def _start_lock_holder(db_path: Path) -> subprocess.Popen[str]:
    """Start a subprocess that holds a read-write DuckDB connection open.

    Parameters
    ----------
    db_path
        Path to the DuckDB database file to lock.

    Returns
    -------
    subprocess.Popen[str]
        Running subprocess. The caller must terminate it.

    Raises
    ------
    AssertionError
        Raised if the subprocess does not report that the lock is ready.
    """
    child = textwrap.dedent(
        f"""
        import duckdb
        import sys

        con = duckdb.connect({str(db_path)!r})
        con.execute("CREATE TABLE IF NOT EXISTS locked_marker(i INT)")
        print("READY", flush=True)
        sys.stdin.read()
        con.close()
        """
    )
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", child],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    ready = proc.stdout.readline().strip()
    assert ready == "READY"
    return proc


def _start_wal_change_holder(db_path: Path) -> subprocess.Popen[str]:
    """Start a subprocess that keeps a WAL-only schema change open.

    Parameters
    ----------
    db_path
        Path to the DuckDB database file to create and hold open.

    Returns
    -------
    subprocess.Popen[str]
        Running subprocess. The caller must terminate it.

    Raises
    ------
    AssertionError
        Raised if the subprocess does not report that the WAL change is ready.
    """
    child = textwrap.dedent(
        f"""
        import duckdb
        import sys

        con = duckdb.connect({str(db_path)!r})
        con.execute("CREATE TABLE wal_only(i INT)")
        print("READY", flush=True)
        sys.stdin.read()
        con.close()
        """
    )
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", child],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    ready = proc.stdout.readline().strip()
    assert ready == "READY"
    return proc


def _stop_process(proc: subprocess.Popen[str]) -> None:
    """Terminate a subprocess and collect its pipes.

    Parameters
    ----------
    proc
        Process to terminate.

    Returns
    -------
    None
        The process is stopped in place.

    Raises
    ------
    None
        Cleanup errors are ignored after termination.
    """
    try:
        if proc.stdin is not None:
            proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
        proc.wait(timeout=5)


def _real_ui_db_fixture() -> Path:
    """Return the real DuckDB UI fixture path or skip if it is absent.

    Parameters
    ----------
    None
        This helper does not accept parameters.

    Returns
    -------
    pathlib.Path
        Path to the real UI-derived ``ui.db`` fixture.

    Raises
    ------
    pytest.skip.Exception
        Raised when the real fixture has not been generated yet.
    """
    fixture_path = Path(__file__).parent / "fixtures" / "ui_db" / "ui.db"
    if not fixture_path.exists():
        pytest.skip("real ui.db fixture not yet generated")
    return fixture_path


def _fetch_notebook_json_values(ui_db_path: Path) -> list[str]:
    """Fetch notebook JSON values from a real UI-derived fixture.

    Parameters
    ----------
    ui_db_path
        Path to the real DuckDB UI database fixture.

    Returns
    -------
    list[str]
        Raw notebook JSON values from ``notebook_versions.json``.

    Raises
    ------
    duckdb.Error
        Raised if the fixture schema no longer matches the assumed UI schema.
    """
    with duckdb.connect(str(ui_db_path), read_only=True) as con:
        rows = con.execute(
            """
            SELECT json
            FROM notebook_versions
            ORDER BY 1
            """
        ).fetchall()
    return [row[0] for row in rows]


def test_at_001_savepoint_is_not_supported() -> None:
    """AT-001: SAVEPOINT fails with ParserException inside a transaction.

    Parameters
    ----------
    None
        This test does not accept parameters.

    Returns
    -------
    None
        The test passes when DuckDB still rejects SAVEPOINT.

    Raises
    ------
    AssertionError
        Raised if DuckDB starts accepting SAVEPOINT.
    """
    con = duckdb.connect()
    try:
        con.execute("BEGIN")
        with pytest.raises(duckdb.ParserException):
            con.execute("SAVEPOINT sp1")
    finally:
        con.close()


def test_at_002_ddl_rolls_back_create_and_drop() -> None:
    """AT-002: DDL CREATE and DROP TABLE are fully rolled back.

    Parameters
    ----------
    None
        This test does not accept parameters.

    Returns
    -------
    None
        The test passes when only the pre-existing table remains.

    Raises
    ------
    AssertionError
        Raised if transactional DDL rollback semantics change.
    """
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE keepme(i INT)")
        con.execute("BEGIN")
        con.execute("CREATE TABLE newtbl(i INT)")
        con.execute("DROP TABLE keepme")
        con.execute("ROLLBACK")

        tables = [row[0] for row in con.execute("SHOW TABLES").fetchall()]
        assert tables == ["keepme"]
    finally:
        con.close()


def test_at_003_error_continuation_and_abort_state() -> None:
    """AT-003: Catalog errors continue but constraint errors abort the transaction.

    Parameters
    ----------
    None
        This test does not accept parameters.

    Returns
    -------
    None
        The test passes when catalog and constraint failures keep their known
        transaction behavior.

    Raises
    ------
    AssertionError
        Raised if DuckDB transaction error propagation changes.
    """
    con = duckdb.connect()
    try:
        con.execute("BEGIN")

        with pytest.raises(duckdb.CatalogException):
            con.execute("SELECT * FROM no_such_table")
        selected = con.execute("SELECT 42").fetchone()
        assert selected is not None
        assert selected == (42,)

        con.execute("CREATE TABLE u(i INT PRIMARY KEY)")
        con.execute("INSERT INTO u VALUES (1)")
        with pytest.raises(duckdb.ConstraintException):
            con.execute("INSERT INTO u VALUES (1)")
        with pytest.raises(duckdb.TransactionException):
            con.execute("SELECT 1").fetchall()
    finally:
        con.close()


def test_at_004_cross_process_read_only_and_read_write_are_locked(
    tmp_path: Path,
) -> None:
    """AT-004: Cross-process access fails while a RW connection holds the DB.

    Parameters
    ----------
    tmp_path
        Temporary directory for the locked database.

    Returns
    -------
    None
        The test passes when read-only and read-write opens both fail with a
        conflicting-lock error.

    Raises
    ------
    AssertionError
        Raised if a second process can open the locked database.
    """
    db_path = tmp_path / "locked.duckdb"
    proc = _start_lock_holder(db_path)
    try:
        read_only = _connect_result(db_path, read_only=True)
        read_write = _connect_result(db_path, read_only=False)
    finally:
        _stop_process(proc)

    assert read_only["status"] == "failed"
    assert read_write["status"] == "failed"
    assert read_only["type"] == "IOException"
    assert read_write["type"] == "IOException"
    assert "Conflicting lock is held" in read_only["message"]
    assert "Conflicting lock is held" in read_write["message"]


def test_at_005_locked_database_body_copy_omits_wal_only_changes(
    tmp_path: Path,
) -> None:
    """AT-005: Copying only the locked DB body misses WAL-only changes.

    Parameters
    ----------
    tmp_path
        Temporary directory for the source and copied database files.

    Returns
    -------
    None
        The test passes when OS copy succeeds and the body-only copy excludes
        the table whose creation is only present through the WAL.

    Raises
    ------
    AssertionError
        Raised if copy behavior or WAL visibility changes.
    """
    db_path = tmp_path / "source.duckdb"
    copy_path = tmp_path / "copy.duckdb"

    proc = _start_wal_change_holder(db_path)
    try:
        wal_path = Path(f"{db_path}.wal")
        assert wal_path.exists()

        shutil.copy(db_path, copy_path)
        copied = duckdb.connect(str(copy_path), read_only=True)
        try:
            copied_tables = copied.execute("SHOW TABLES").fetchall()
        finally:
            copied.close()
    finally:
        _stop_process(proc)

    assert copied_tables == []


def test_at_006_copy_to_file_survives_rollback(tmp_path: Path) -> None:
    """AT-006: COPY TO output remains on disk after transaction rollback.

    Parameters
    ----------
    tmp_path
        Temporary directory for the exported CSV file.

    Returns
    -------
    None
        The test passes when the external file still exists after ROLLBACK.

    Raises
    ------
    AssertionError
        Raised if DuckDB starts rolling back external file writes.
    """
    out_path = tmp_path / "leak.csv"
    con = duckdb.connect()
    try:
        con.execute("BEGIN")
        con.execute(f"COPY (SELECT 1 AS a) TO '{out_path}' (FORMAT CSV)")
        con.execute("ROLLBACK")
    finally:
        con.close()

    assert out_path.exists()
    assert out_path.read_text(encoding="utf-8").strip() == "a\n1"


def test_at_007_interrupt_stops_query_and_connection_remains_usable() -> None:
    """AT-007: connection.interrupt stops a query and leaves the connection usable.

    Parameters
    ----------
    None
        This test does not accept parameters.

    Returns
    -------
    None
        The test passes when an interrupted query raises InterruptException and
        the same connection can still execute a later statement.

    Raises
    ------
    AssertionError
        Raised if interrupt semantics change.
    """
    con = duckdb.connect()
    timer = threading.Timer(0.1, con.interrupt)
    try:
        timer.start()
        with pytest.raises(duckdb.InterruptException):
            con.execute(
                "SELECT count(*) FROM range(10000000000) a, range(100) b"
            ).fetchall()
        selected = con.execute("SELECT 1").fetchone()
        assert selected is not None
        assert selected == (1,)
    finally:
        timer.cancel()
        con.close()


def test_at_008_transaction_control_statement_support_inside_transaction(
    tmp_path: Path,
) -> None:
    """AT-008: ATTACH, CHECKPOINT, and SET work in a transaction but BEGIN fails.

    Parameters
    ----------
    tmp_path
        Temporary directory for the ATTACH target database.

    Returns
    -------
    None
        The test passes when transaction-control assumptions match the design.

    Raises
    ------
    AssertionError
        Raised if statement support inside transactions changes.
    """
    db2 = tmp_path / "other.duckdb"
    con = duckdb.connect()
    try:
        for stmt in [f"ATTACH '{db2}' AS other", "CHECKPOINT", "SET threads=2"]:
            con.execute("BEGIN")
            con.execute(stmt)
            con.execute("ROLLBACK")

        con.execute("BEGIN")
        with pytest.raises(duckdb.TransactionException):
            con.execute("BEGIN")
        con.execute("ROLLBACK")
    finally:
        con.close()


def test_at_009_real_ui_db_contains_expected_notebook_schema() -> None:
    """AT-009: Real ui.db exposes notebooks, notebook_versions, and json.

    Parameters
    ----------
    None
        This test does not accept parameters.

    Returns
    -------
    None
        The test passes when the real UI-derived fixture preserves the assumed
        table and column names.

    Raises
    ------
    AssertionError
        Raised if the UI schema no longer matches Reader assumptions.
    """
    ui_db_path = _real_ui_db_fixture()

    with duckdb.connect(str(ui_db_path), read_only=True) as con:
        tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
        version_columns = {
            row[1]
            for row in con.execute("PRAGMA table_info('notebook_versions')").fetchall()
        }
        json_values = con.execute(
            "SELECT json FROM notebook_versions LIMIT 1"
        ).fetchall()

    assert {"notebooks", "notebook_versions"} <= tables
    assert "json" in version_columns
    assert json_values


def test_at_010_real_ui_db_notebook_json_parses_as_model() -> None:
    """AT-010: Real ui.db notebook JSON parses into the StoredNotebook model.

    Parameters
    ----------
    None
        This test does not accept parameters.

    Returns
    -------
    None
        The test passes when all fixture notebook JSON values validate against
        the current Pydantic model.

    Raises
    ------
    AssertionError
        Raised if a fixture contains no notebook JSON records.
    """
    ui_db_path = _real_ui_db_fixture()
    json_values = _fetch_notebook_json_values(ui_db_path)

    assert json_values
    for raw_json in json_values:
        StoredNotebook.model_validate(json.loads(raw_json))
