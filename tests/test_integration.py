"""Integration tests for DuckDB UI notebook HTML export.

Parameters
----------
None
    This module does not accept parameters.

Returns
-------
None
    Importing this module registers pytest integration tests.

Raises
------
None
    Importing this module should not raise package-specific exceptions.

Notes
-----
Tests in this module intentionally exercise real DuckDB connections and real
process boundaries where required by the integration test design.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest

from duckdb_ui_notebook_export.executor import CellStatus, execute_notebook
from duckdb_ui_notebook_export.models import Cell, Notebook
from duckdb_ui_notebook_export.reader import load_notebook, open_ui_db
from duckdb_ui_notebook_export.renderer import ExportMetadata, render_html

pytestmark = pytest.mark.integration


@pytest.fixture
def synthetic_ui_db(tmp_path: Path) -> Path:
    """Build a synthetic ui.db, skipping with the builder's unsupported reason.

    Parameters
    ----------
    tmp_path
        Temporary directory where the synthetic database should be generated.

    Returns
    -------
    pathlib.Path
        Path to a generated ui.db file.

    Raises
    ------
    pytest.skip.Exception
        Raised by pytest when the builder cannot encode a requested cell type.

    Notes
    -----
    This fixture is used only for integration cases that must pass through the
    Reader layer and therefore depend on the unofficial DuckDB UI schema.
    """
    from tests.helpers.synthetic_ui_db import build_ui_db

    try:
        return build_ui_db(
            [
                {
                    "name": "integration-notebook",
                    "notebook_id": "nb-integration",
                    "versions": [
                        {
                            "version_id": "nb-integration-v1",
                            "created_at": "2026-07-05T00:00:00Z",
                            "cells": [{"cell_type": "sql", "sql": "SELECT 1"}],
                        }
                    ],
                }
            ],
            tmp_path,
        )
    except NotImplementedError as error:
        pytest.skip(str(error))


@pytest.fixture
def locked_ui_db(synthetic_ui_db: Path) -> Iterator[Path]:
    """Hold a real DuckDB read-write connection in a subprocess.

    Parameters
    ----------
    synthetic_ui_db
        Synthetic DuckDB UI database path to lock from another process.

    Returns
    -------
    collections.abc.Iterator[pathlib.Path]
        Iterator yielding the locked database path.

    Raises
    ------
    RuntimeError
        Raised when the child process exits before signalling readiness.

    Notes
    -----
    The child process uses a read-write DuckDB connection and keeps a
    transaction open until pytest tears the fixture down.
    """
    code = textwrap.dedent(
        """
        import sys
        import time

        import duckdb

        connection = duckdb.connect(sys.argv[1], read_only=False)
        connection.execute("BEGIN TRANSACTION")
        connection.execute("CREATE TABLE IF NOT EXISTS lock_holder(value INTEGER)")
        print("ready", flush=True)
        try:
            while True:
                time.sleep(0.1)
        finally:
            connection.close()
        """,
    )
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", code, str(synthetic_ui_db)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    ready = process.stdout.readline().strip()
    if ready != "ready":
        stderr = process.stderr.read() if process.stderr is not None else ""
        process.kill()
        raise RuntimeError(f"lock holder did not start: {stderr}")
    try:
        yield synthetic_ui_db
    finally:
        process.terminate()
        process.wait(timeout=5)


def _notebook(cells: list[str]) -> Notebook:
    """Build a notebook model from SQL cell strings.

    Parameters
    ----------
    cells
        SQL strings to place into notebook cells.

    Returns
    -------
    duckdb_ui_notebook_export.models.Notebook
        Notebook containing the requested SQL cells.

    Raises
    ------
    pydantic.ValidationError
        Raised if the test data does not satisfy the model contract.

    Notes
    -----
    Integration cases that target Executor and Renderer coupling do not need a
    DuckDB UI database because the public contract accepts a Notebook model.
    """
    return Notebook(
        name="integration-notebook",
        version_id="it-version",
        cells=[Cell(sql=sql) for sql in cells],
    )


def _metadata(notebook: Notebook, warnings: list[str] | None = None) -> ExportMetadata:
    """Build stable export metadata for rendering assertions.

    Parameters
    ----------
    notebook
        Notebook whose version identifier should be embedded.
    warnings
        Optional warnings to include in metadata.

    Returns
    -------
    duckdb_ui_notebook_export.renderer.ExportMetadata
        Metadata object with deterministic values.

    Raises
    ------
    None
        This helper does not raise package-specific exceptions.

    Notes
    -----
    Deterministic metadata keeps integration assertions focused on execution
    and rendering behavior rather than wall-clock values.
    """
    return ExportMetadata(
        exported_at_utc="2026-07-05T00:00:00Z",
        duckdb_version=duckdb.__version__,
        notebook_version_id=notebook.version_id,
        tool_version="0.1.0",
        warnings=warnings or [],
    )


def _execute_and_render(
    notebook: Notebook,
    db_path: Path,
    **execute_kwargs: Any,
) -> str:
    """Execute a notebook and render the resulting HTML.

    Parameters
    ----------
    notebook
        Notebook to execute.
    db_path
        Target DuckDB database path.
    **execute_kwargs
        Keyword arguments forwarded to ``execute_notebook``.

    Returns
    -------
    str
        Rendered HTML for the executed notebook.

    Raises
    ------
    NotImplementedError
        Raised while the production executor or renderer remains unimplemented.

    Notes
    -----
    This is the narrow integration boundary for Executor to Renderer tests.
    """
    report = execute_notebook(notebook, str(db_path), **execute_kwargs)
    return render_html(notebook, report, _metadata(notebook, report.warnings))


def test_it_001_snapshot_reader_succeeds_while_rw_process_holds_ui_db(
    locked_ui_db: Path,
) -> None:
    """IT-001: Snapshot reading succeeds while another process holds RW ui.db.

    Parameters
    ----------
    locked_ui_db
        Synthetic UI database locked by a read-write subprocess.

    Returns
    -------
    None
        The assertion verifies the integration contract.

    Raises
    ------
    NotImplementedError
        Raised while the production reader remains unimplemented.

    Notes
    -----
    The synthetic UI database fixture skips this test until design doc 6.2#1 is
    resolved.
    """
    notebook = load_notebook(locked_ui_db, "integration-notebook")

    assert notebook.name == "integration-notebook"


def test_it_002_direct_reader_fails_while_rw_process_holds_ui_db(
    locked_ui_db: Path,
) -> None:
    """IT-002: Direct ui.db reading fails while another process holds RW ui.db.

    Parameters
    ----------
    locked_ui_db
        Synthetic UI database locked by a read-write subprocess.

    Returns
    -------
    None
        The assertion verifies the integration contract.

    Raises
    ------
    duckdb_ui_notebook_export.exceptions.UiDbAccessError
        Expected when the implementation maps direct access lock failures.

    Notes
    -----
    This is the direct-access control case for IT-001 and is skipped until the
    synthetic UI schema helper is implemented.
    """
    with pytest.raises(Exception, match=r"lock|access|busy|closed"):
        open_ui_db(locked_ui_db, require_ui_closed=True)


def test_it_003_error_cell_rendered_and_following_cell_continues(
    fresh_duckdb: Path,
) -> None:
    """IT-003: Non-abort errors render as errors and later cells render normally.

    Parameters
    ----------
    fresh_duckdb
        Temporary DuckDB database path used for real execution.

    Returns
    -------
    None
        The assertion verifies the rendered HTML.

    Raises
    ------
    NotImplementedError
        Raised while the production executor or renderer remains unimplemented.

    Notes
    -----
    A missing table error should not abort the transaction, so the final SELECT
    must still be executed and rendered.
    """
    html = _execute_and_render(
        _notebook(["SELECT * FROM missing_table", "SELECT 42 AS answer"]),
        fresh_duckdb,
    )

    assert "missing_table" in html
    assert "42" in html
    assert "answer" in html


def test_it_004_abort_error_marks_following_cells_as_skipped(
    fresh_duckdb: Path,
) -> None:
    """IT-004: Abort errors render and following cells render as skipped.

    Parameters
    ----------
    fresh_duckdb
        Temporary DuckDB database path used for real execution.

    Returns
    -------
    None
        The assertion verifies the rendered HTML.

    Raises
    ------
    NotImplementedError
        Raised while the production executor or renderer remains unimplemented.

    Notes
    -----
    A primary-key violation is expected to place the transaction in an aborted
    state, causing subsequent cells to be skipped.
    """
    notebook = _notebook(
        [
            "CREATE TABLE items(id INTEGER PRIMARY KEY)",
            "INSERT INTO items VALUES (1), (1)",
            "SELECT 99 AS should_be_skipped",
        ],
    )
    report = execute_notebook(notebook, str(fresh_duckdb))
    html = render_html(notebook, report, _metadata(notebook, report.warnings))

    assert report.cell_results[1].status is CellStatus.ERROR
    assert report.cell_results[2].status is CellStatus.SKIPPED_ABORT
    assert "skipped" in html.lower()
    assert "transaction" in html.lower()


def test_it_005_timeout_cell_rendered_as_failure(fresh_duckdb: Path) -> None:
    """IT-005: Interrupted timeout cells are rendered as failures.

    Parameters
    ----------
    fresh_duckdb
        Temporary DuckDB database path used for real execution.

    Returns
    -------
    None
        The assertion verifies the rendered HTML.

    Raises
    ------
    NotImplementedError
        Raised while the production executor or renderer remains unimplemented.

    Notes
    -----
    The timeout values are intentionally small so the test never waits for the
    production defaults of 300 seconds and 30 seconds.
    """
    html = _execute_and_render(
        _notebook(
            [
                "SELECT sum(a.range * b.range) "
                "FROM range(100000000) a, range(100000000) b",
            ],
        ),
        fresh_duckdb,
        cell_timeout=0.1,
        interrupt_grace=2.0,
    )

    assert "timeout" in html.lower()
    assert "failed" in html.lower() or "error" in html.lower()


def test_it_006_allow_writes_commits_changes_to_target_db(fresh_duckdb: Path) -> None:
    """IT-006: Allow-writes commits notebook changes to the target database.

    Parameters
    ----------
    fresh_duckdb
        Temporary DuckDB database path used for real execution.

    Returns
    -------
    None
        The assertion verifies persistent DuckDB state after export execution.

    Raises
    ------
    NotImplementedError
        Raised while the production executor remains unimplemented.

    Notes
    -----
    The postcondition is checked through a new real DuckDB connection.
    """
    execute_notebook(
        _notebook(
            [
                "CREATE TABLE committed(value INTEGER)",
                "INSERT INTO committed VALUES (7)",
            ],
        ),
        str(fresh_duckdb),
        allow_writes=True,
    )

    with duckdb.connect(str(fresh_duckdb), read_only=True) as connection:
        assert connection.execute("SELECT value FROM committed").fetchall() == [(7,)]


def test_it_007_default_execution_rolls_back_changes(fresh_duckdb: Path) -> None:
    """IT-007: Default execution rolls back changes to the target database.

    Parameters
    ----------
    fresh_duckdb
        Temporary DuckDB database path used for real execution.

    Returns
    -------
    None
        The assertion verifies that notebook writes are not persisted.

    Raises
    ------
    NotImplementedError
        Raised while the production executor remains unimplemented.

    Notes
    -----
    The query is expected to fail after execution because the table creation
    should have been rolled back.
    """
    execute_notebook(
        _notebook(
            [
                "CREATE TABLE rolled_back(value INTEGER)",
                "INSERT INTO rolled_back VALUES (7)",
            ],
        ),
        str(fresh_duckdb),
    )

    with (
        duckdb.connect(str(fresh_duckdb), read_only=True) as connection,
        pytest.raises(duckdb.CatalogException),
    ):
        connection.execute("SELECT value FROM rolled_back").fetchall()


def test_it_008_copy_to_file_survives_default_rollback(
    fresh_duckdb: Path,
    tmp_path: Path,
) -> None:
    """IT-008: COPY TO output files remain after default rollback execution.

    Parameters
    ----------
    fresh_duckdb
        Temporary DuckDB database path used for real execution.
    tmp_path
        Temporary directory used for COPY output.

    Returns
    -------
    None
        The assertion verifies the external file side effect.

    Raises
    ------
    NotImplementedError
        Raised while the production executor remains unimplemented.

    Notes
    -----
    DuckDB file output is an external side effect and is not undone by rolling
    back the transaction.
    """
    copy_path = tmp_path / "copy-output.csv"
    copy_sql = (
        f"COPY (SELECT 1 AS value) TO '{copy_path.as_posix()}' (HEADER, DELIMITER ',')"
    )
    execute_notebook(
        _notebook([copy_sql]),
        str(fresh_duckdb),
    )

    assert copy_path.read_text(encoding="utf-8") == "value\n1\n"


def test_it_009_cli_requires_prompt_for_copy_to_side_effect(
    tmp_path: Path,
) -> None:
    """IT-009: CLI follows the confirmation prompt path for COPY TO notebooks.

    Parameters
    ----------
    tmp_path
        Temporary directory used for CLI output.

    Returns
    -------
    None
        The assertion verifies the CLI confirmation behavior.

    Raises
    ------
    pytest.skip.Exception
        Raised if the synthetic UI database helper cannot encode test data.

    Notes
    -----
    The skip reason comes from the synthetic database builder when a requested
    cell type is not represented in stored format v3.
    """
    from tests.helpers.synthetic_ui_db import build_ui_db

    copy_path = tmp_path / "copy-output.csv"
    copy_sql = (
        f"COPY (SELECT 1 AS value) TO '{copy_path.as_posix()}' (HEADER, DELIMITER ',')"
    )
    try:
        synthetic_ui_db = build_ui_db(
            [
                {
                    "name": "copy-notebook",
                    "notebook_id": "nb-copy",
                    "versions": [
                        {
                            "version_id": "nb-copy-v1",
                            "created_at": "2026-07-05T00:00:00Z",
                            "cells": [{"cell_type": "sql", "sql": copy_sql}],
                        }
                    ],
                }
            ],
            tmp_path,
        )
    except NotImplementedError as error:
        pytest.skip(str(error))

    output_path = tmp_path / "copy.html"
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "duckdb_ui_notebook_export.cli",
            "copy-notebook",
            "--ui-db",
            str(synthetic_ui_db),
            "--output",
            str(output_path),
            "--output-dir",
            str(output_path.parent),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 5
    assert "confirm" in result.stderr.lower() or "confirm" in result.stdout.lower()


def test_it_010_require_ui_closed_fails_when_rw_process_holds_ui_db(
    locked_ui_db: Path,
    tmp_path: Path,
) -> None:
    """IT-010: require-ui-closed returns exit code 4 when RW ui.db is held.

    Parameters
    ----------
    locked_ui_db
        Synthetic UI database locked by a read-write subprocess.
    tmp_path
        Temporary directory used for CLI output.

    Returns
    -------
    None
        The assertion verifies the CLI exit-code contract.

    Raises
    ------
    pytest.skip.Exception
        Raised while the synthetic UI database helper is blocked.

    Notes
    -----
    This test intentionally drives the CLI through a subprocess to preserve the
    process-boundary lock behavior.
    """
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "duckdb_ui_notebook_export.cli",
            "integration-notebook",
            "--ui-db",
            str(locked_ui_db),
            "--require-ui-closed",
            "--output",
            str(tmp_path / "locked.html"),
            "--output-dir",
            str(tmp_path),
            "--yes",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 4
    assert "ui.db" in result.stderr.lower()
