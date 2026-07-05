"""Unit tests for HTML rendering.

Parameters
----------
None
    This module does not accept parameters.

Returns
-------
None
    Importing this module registers renderer tests.

Raises
------
None
    Importing this module should not raise package-specific exceptions.

Notes
-----
These tests encode the renderer contract from design document section 3.3.
"""

import re

from duckdb_ui_notebook_export.executor import (
    CellResult,
    CellStatus,
    ExecutionReport,
)
from duckdb_ui_notebook_export.models import Cell, Notebook
from duckdb_ui_notebook_export.renderer import (
    ExportMetadata,
    mask_secrets,
    render_html,
    truncate_value,
)


def _notebook(sql: str, *, cell_type: str = "sql") -> Notebook:
    """Build a one-cell notebook for renderer tests.

    Parameters
    ----------
    sql
        SQL text to store in the cell.
    cell_type
        Notebook cell type to assign to the cell.

    Returns
    -------
    duckdb_ui_notebook_export.models.Notebook
        Notebook model suitable for direct renderer input.

    Raises
    ------
    pydantic.ValidationError
        Raised when the model contract rejects the supplied values.

    Notes
    -----
    The renderer tests construct models directly and do not depend on ui.db.
    """
    return Notebook(
        name="Renderer Contract",
        version_id="nb-version-123",
        cells=[Cell(cell_type=cell_type, sql=sql)],
    )


def _metadata() -> ExportMetadata:
    """Build stable export metadata for renderer tests.

    Returns
    -------
    duckdb_ui_notebook_export.renderer.ExportMetadata
        Metadata object with deterministic values.

    Notes
    -----
    Fixed values keep string assertions independent from wall-clock time.
    """
    return ExportMetadata(
        exported_at_utc="2026-07-05T00:00:00Z",
        duckdb_version="v1.5.4",
        notebook_version_id="nb-version-123",
        tool_version="0.1.0",
        warnings=[],
    )


def _report(result: CellResult) -> ExecutionReport:
    """Build a single-cell execution report.

    Parameters
    ----------
    result
        Cell result to place in the execution report.

    Returns
    -------
    duckdb_ui_notebook_export.executor.ExecutionReport
        Execution report containing the provided result.

    Notes
    -----
    Renderer unit tests should not execute SQL.
    """
    return ExecutionReport(
        cell_results=[result],
        warnings=[],
        used_memory_fallback=False,
    )


def _ok_result(
    rows: list[tuple],
    *,
    columns: list[str] | None = None,
    truncated: bool = False,
) -> CellResult:
    """Build a successful query result.

    Parameters
    ----------
    rows
        Result rows to render.
    columns
        Optional column names. A single ``value`` column is used by default.
    truncated
        Whether the executor detected more rows than the display limit.

    Returns
    -------
    duckdb_ui_notebook_export.executor.CellResult
        Successful cell result.

    Notes
    -----
    ``affected_rows`` is left unset to model a SELECT-style result.
    """
    return CellResult(
        status=CellStatus.OK,
        columns=columns or ["value"],
        rows=rows,
        truncated=truncated,
        affected_rows=None,
        error_message=None,
    )


def _html_for(
    sql: str,
    result: CellResult,
    *,
    cell_type: str = "sql",
) -> str:
    """Render a one-cell notebook to HTML.

    Parameters
    ----------
    sql
        SQL text stored in the notebook cell.
    result
        Execution result for the cell.
    cell_type
        Notebook cell type to assign to the cell.

    Returns
    -------
    str
        Rendered HTML document.

    Notes
    -----
    This helper is intentionally thin so each test still asserts one contract.
    """
    return render_html(
        _notebook(sql, cell_type=cell_type), _report(result), _metadata()
    )


def test_ut_rd_001_cell_values_are_html_escaped() -> None:
    """UT-RD-001: Cell values with HTML tags are escaped."""
    html = _html_for(
        "SELECT '<script>alert(1)</script>' AS value",
        _ok_result([("<script>alert(1)</script>",)]),
    )

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html


def test_ut_rd_002_cell_sql_is_html_escaped() -> None:
    """UT-RD-002: Cell SQL containing HTML-sensitive characters is escaped."""
    html = _html_for(
        "SELECT 1 < 2 AND name = 'a&b' AND note = '<tag>'",
        _ok_result([(True,)], columns=["ok"]),
    )

    assert "SELECT 1 &lt; 2" in html
    assert "a&amp;b" in html
    assert "&lt;tag&gt;" in html
    assert "SELECT 1 < 2" not in html


def test_ut_rd_003_create_secret_parameter_values_are_masked() -> None:
    """UT-RD-003: CREATE SECRET parameter values are masked."""
    sql = (
        "CREATE SECRET my_secret "
        "(TYPE s3, PROVIDER credential_chain, KEY_ID 'AKIA...', "
        "SECRET 'xyz', REGION 'us-east-1')"
    )

    masked = mask_secrets(sql)

    assert "KEY_ID ***" in masked
    assert "SECRET ***" in masked
    assert "REGION ***" in masked
    assert "AKIA" not in masked
    assert "xyz" not in masked
    assert "us-east-1" not in masked


def test_ut_rd_004_create_secret_structural_elements_remain_visible() -> None:
    """UT-RD-004: CREATE SECRET structural elements remain visible."""
    sql = (
        "CREATE SECRET my_secret "
        "(TYPE s3, PROVIDER credential_chain, KEY_ID 'AKIA...', "
        "SECRET 'xyz', REGION 'us-east-1')"
    )

    masked = mask_secrets(sql)

    assert "CREATE SECRET my_secret" in masked
    assert "TYPE s3" in masked
    assert "PROVIDER credential_chain" in masked


def test_ut_rd_005_null_values_are_displayed_as_null() -> None:
    """UT-RD-005: NULL values are displayed explicitly."""
    html = _html_for("SELECT NULL AS value", _ok_result([(None,)]))

    assert "NULL" in html


def test_ut_rd_006_struct_values_use_duckdb_string_representation() -> None:
    """UT-RD-006: STRUCT values use DuckDB string representation."""
    value = "{'id': 42, 'name': 'Ada'}"
    html = _html_for(
        "SELECT {'id': 42, 'name': 'Ada'} AS value", _ok_result([(value,)])
    )

    assert value in html


def test_ut_rd_007_list_values_use_duckdb_string_representation() -> None:
    """UT-RD-007: LIST values use DuckDB string representation."""
    value = "[1, 2, 3]"
    html = _html_for("SELECT [1, 2, 3] AS value", _ok_result([(value,)]))

    assert value in html


def test_ut_rd_008_map_values_use_duckdb_string_representation() -> None:
    """UT-RD-008: MAP values use DuckDB string representation."""
    value = "{'alpha': 1, 'beta': 2}"
    html = _html_for(
        "SELECT MAP {'alpha': 1, 'beta': 2} AS value",
        _ok_result([(value,)]),
    )

    assert value in html


def test_ut_rd_009_blob_values_show_size_instead_of_bytes() -> None:
    """UT-RD-009: BLOB values are rendered as their byte size."""
    blob_value = b"\x00\x01private"
    html = _html_for("SELECT payload FROM blobs", _ok_result([(blob_value,)]))

    assert "9 bytes" in html
    assert "private" not in html


def test_ut_rd_010_long_values_are_truncated_with_original_length() -> None:
    """UT-RD-010: Values longer than 500 characters are truncated."""
    value = "x" * 501

    truncated = truncate_value(value)

    assert truncated.startswith("x" * 500)
    assert len(truncated) > 500
    assert "501" in truncated


def test_ut_rd_011_short_values_are_not_truncated() -> None:
    """UT-RD-011: Values up to 500 characters are rendered in full."""
    value = "x" * 500

    assert truncate_value(value) == value


def test_ut_rd_012_truncated_result_sets_report_display_limit() -> None:
    """UT-RD-012: Result sets over 1,000 rows show a display-limit note."""
    rows = [(index,) for index in range(1000)]
    html = _html_for("SELECT * FROM large_table", _ok_result(rows, truncated=True))

    assert "first 1,000 rows" in html
    assert "more than 1,000 rows" in html
    assert "total row count was not computed" in html


def test_ut_rd_013_statements_without_result_sets_show_ok() -> None:
    """UT-RD-013: Statements without result sets show completion."""
    result = CellResult(
        status=CellStatus.OK,
        columns=[],
        rows=[],
        truncated=False,
        affected_rows=None,
        error_message=None,
    )

    html = _html_for("CREATE TABLE t (id INTEGER)", result)

    assert "OK" in html


def test_ut_rd_014_dml_statements_show_affected_rows() -> None:
    """UT-RD-014: DML statements show affected row counts."""
    result = CellResult(
        status=CellStatus.OK,
        columns=[],
        rows=[],
        truncated=False,
        affected_rows=3,
        error_message=None,
    )

    html = _html_for("UPDATE items SET active = false", result)

    assert "3" in html
    assert "affected row" in html.lower()


def test_ut_rd_015_chart_cells_render_table_with_english_note() -> None:
    """UT-RD-015: Chart cells render table fallback with an English note."""
    html = _html_for(
        "SELECT category, total FROM chart_data",
        _ok_result([("a", 10)], columns=["category", "total"]),
        cell_type="chart",
    )

    assert "a" in html
    assert "10" in html
    assert "Chart rendering is not supported in Phase 1" in html
    assert "shown as a table" in html


def test_ut_rd_016_skipped_abort_cells_show_english_reason() -> None:
    """UT-RD-016: Skipped abort cells show the transaction-abort reason."""
    result = CellResult(
        status=CellStatus.SKIPPED_ABORT,
        columns=[],
        rows=[],
        truncated=False,
        affected_rows=None,
        error_message=None,
    )

    html = _html_for("SELECT 1", result)

    assert "Skipped because the transaction was aborted" in html


def test_ut_rd_017_metadata_includes_export_timestamp_utc() -> None:
    """UT-RD-017: Metadata includes the UTC export timestamp."""
    html = _html_for("SELECT 1", _ok_result([(1,)]))

    assert "2026-07-05T00:00:00Z" in html
    assert "UTC" in html


def test_ut_rd_018_metadata_includes_duckdb_version() -> None:
    """UT-RD-018: Metadata includes the DuckDB version."""
    html = _html_for("SELECT 1", _ok_result([(1,)]))

    assert "DuckDB" in html
    assert "v1.5.4" in html


def test_ut_rd_019_metadata_includes_notebook_version_id() -> None:
    """UT-RD-019: Metadata includes the notebook version identifier."""
    html = _html_for("SELECT 1", _ok_result([(1,)]))

    assert "nb-version-123" in html


def test_ut_rd_020_metadata_includes_tool_version() -> None:
    """UT-RD-020: Metadata includes the export tool version."""
    html = _html_for("SELECT 1", _ok_result([(1,)]))

    assert "0.1.0" in html


def test_ut_rd_021_html_has_no_external_resource_references() -> None:
    """UT-RD-021: HTML has no external resource references."""
    html = _html_for(
        "SELECT 'https://example.com/data.csv' AS url",
        _ok_result([("https://example.com/data.csv",)], columns=["url"]),
    )

    assert "https://example.com/data.csv" in html
    assert not re.search(r"<link\s+[^>]*href\s*=", html, flags=re.IGNORECASE)
    assert not re.search(r"<script\s+[^>]*src\s*=", html, flags=re.IGNORECASE)
    assert not re.search(r"<img\s+[^>]*src\s*=", html, flags=re.IGNORECASE)
    assert "@import" not in html
    assert "url(" not in html


def test_ut_rd_022_notes_and_errors_use_english_text() -> None:
    """UT-RD-022: Notes and error messages use English text."""
    result = CellResult(
        status=CellStatus.ERROR,
        columns=[],
        rows=[],
        truncated=False,
        affected_rows=None,
        error_message="Table not found: missing_table",
    )
    html = _html_for("SELECT * FROM missing_table", result)

    assert "Table not found: missing_table" in html
    assert not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", html)


def test_ut_rd_023_css_includes_prefers_color_scheme() -> None:
    """UT-RD-023: Generated CSS includes prefers-color-scheme."""
    html = _html_for("SELECT 1", _ok_result([(1,)]))

    assert "prefers-color-scheme" in html
