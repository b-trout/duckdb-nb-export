"""End-to-end golden HTML tests for the DuckDB UI notebook exporter.

Parameters
----------
None
    This module does not accept parameters.

Returns
-------
None
    Importing this module registers pytest end-to-end tests.

Raises
------
None
    Importing this module should not raise package-specific exceptions.

Notes
-----
The golden fixtures and synthetic UI database builder are intentionally wired
now, with unsupported stored-format scenarios skipped by the builder.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


def _build_synthetic_ui_db(
    tmp_path: Path,
    notebook_name: str,
    notebook_id: str,
    version_id: str,
    cells: list[dict[str, str]],
) -> Path:
    """Build a scenario-specific synthetic ui.db.

    Parameters
    ----------
    tmp_path
        Temporary directory where the synthetic database should be generated.
    notebook_name
        Notebook name to encode in the synthetic database.
    notebook_id
        Stable notebook identifier to encode in the synthetic database.
    version_id
        Stable notebook version identifier to encode in the synthetic database.
    cells
        Ordered notebook cells for the E2E scenario.

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
    Each E2E test passes the exact notebook cells needed for its scenario while
    relying on the builder to skip unsupported stored-format v3 cell types.
    """
    from tests.helpers.synthetic_ui_db import build_ui_db

    notebooks = [
        {
            "name": notebook_name,
            "notebook_id": notebook_id,
            "updated_at": "2026-07-05T00:00:00Z",
            "versions": [
                {
                    "version_id": version_id,
                    "created_at": "2026-07-05T00:00:00Z",
                    "cells": cells,
                },
            ],
        },
    ]

    try:
        return build_ui_db(notebooks, tmp_path)
    except NotImplementedError as error:
        pytest.skip(str(error))


def normalize_golden_html(html: str) -> str:
    """Normalize variable export metadata for golden HTML comparison.

    Parameters
    ----------
    html
        Raw exported HTML.

    Returns
    -------
    str
        HTML with volatile metadata replaced by stable placeholders.

    Raises
    ------
    None
        This helper does not raise package-specific exceptions.

    Notes
    -----
    The four placeholders come from design document section 2.3: export
    timestamp, DuckDB version, notebook version ID, and tool version.
    """
    normalized = re.sub(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z",
        "__EXPORT_TIMESTAMP__",
        html,
    )
    normalized = re.sub(
        r"DuckDB(?:\s+version)?(?:\s*[:=]\s*|\s+)v?\d+(?:\.\d+)+(?:[-+.\w]*)?",
        "DuckDB version: __DUCKDB_VERSION__",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"(?:notebook[-_\s]?version(?:[-_\s]?id)?|nb[-_\s]?version[-_\s]?id)"
        r"(?:\s*[:=]\s*|\s+)['\"]?[A-Za-z0-9_.:-]+['\"]?",
        "notebook version id: __NB_VERSION_ID__",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"(?:tool[-_\s]?version|exporter[-_\s]?version)(?:\s*[:=]\s*|\s+)['\"]?"
        r"v?\d+(?:\.\d+)+(?:[-+.\w]*)?['\"]?",
        "tool version: __TOOL_VERSION__",
        normalized,
        flags=re.IGNORECASE,
    )
    return normalized


def _run_cli_export(
    synthetic_ui_db: Path,
    tmp_path: Path,
    notebook_name: str,
    *extra_args: str,
) -> Path:
    """Run the exporter CLI as a real Python subprocess.

    Parameters
    ----------
    synthetic_ui_db
        Synthetic DuckDB UI database path.
    tmp_path
        Temporary directory where the output HTML should be written.
    notebook_name
        Notebook name to request from the CLI.
    *extra_args
        Additional command-line arguments to pass to the CLI.

    Returns
    -------
    pathlib.Path
        Path to the generated HTML file.

    Raises
    ------
    AssertionError
        Raised if the CLI return code differs from the expected success code.

    Notes
    -----
    The CLI is invoked via ``sys.executable -m`` to test the installed package
    module path without depending on a console script being installed.
    ``--output-dir`` is passed explicitly (pointed at ``tmp_path``) because the
    allowed base directory must be changed via that option rather than relying
    on any implicit escape from the current directory.
    """
    output_path = tmp_path / f"{notebook_name}.html"
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "duckdb_ui_notebook_export.cli",
            notebook_name,
            "--ui-db",
            str(synthetic_ui_db),
            "--output",
            str(output_path),
            "--output-dir",
            str(tmp_path),
            "--yes",
            *extra_args,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return output_path


def _assert_matches_golden(output_path: Path, golden_name: str) -> None:
    """Compare or update a normalized golden HTML fixture.

    Parameters
    ----------
    output_path
        Generated HTML path.
    golden_name
        Filename under ``tests/golden``.

    Returns
    -------
    None
        The assertion verifies normalized HTML equality.

    Raises
    ------
    FileNotFoundError
        Raised if the requested golden fixture has not been created.
    AssertionError
        Raised if normalized generated HTML differs from the golden fixture.

    Notes
    -----
    Golden fixtures are expected to store already-normalized placeholders.
    Set ``UPDATE_GOLDEN_HTML=1`` to write the normalized output to
    ``tests/golden/<golden_name>`` before comparing it. For example, run
    ``UPDATE_GOLDEN_HTML=1 uv run pytest tests/test_e2e.py`` to refresh the
    E2E golden HTML snapshots.
    """
    actual = normalize_golden_html(output_path.read_text(encoding="utf-8"))
    golden_path = Path(__file__).parent / "golden" / golden_name
    if os.environ.get("UPDATE_GOLDEN_HTML") == "1":
        golden_path.write_text(actual, encoding="utf-8")
        warnings.warn(
            f"Updated golden HTML fixture: {golden_path}",
            pytest.PytestWarning,
            stacklevel=2,
        )

    expected = golden_path.read_text(encoding="utf-8")

    assert actual == expected
    assert "<link href=" not in actual
    assert "<script src=" not in actual
    assert "<img src=" not in actual
    assert "@import" not in actual
    assert "url(" not in actual


def test_normalize_golden_html_replaces_variable_metadata() -> None:
    """Normalize helper replaces the four design-doc volatile metadata fields.

    Parameters
    ----------
    None
        This test does not accept parameters.

    Returns
    -------
    None
        The assertion verifies helper behavior.

    Raises
    ------
    AssertionError
        Raised if placeholder replacement does not match the contract.

    Notes
    -----
    This helper self-test has no traceability ID because it verifies local
    golden comparison infrastructure rather than an E2E scenario.
    """
    html = (
        "Exported at 2026-07-05T12:34:56.123Z\n"
        "DuckDB version: v1.5.4\n"
        "notebook version id: abc-123\n"
        "tool version: 0.1.0\n"
    )

    assert normalize_golden_html(html) == (
        "Exported at __EXPORT_TIMESTAMP__\n"
        "DuckDB version: __DUCKDB_VERSION__\n"
        "notebook version id: __NB_VERSION_ID__\n"
        "tool version: __TOOL_VERSION__\n"
    )


def test_e2e_001_successful_cells_match_golden_html(
    tmp_path: Path,
) -> None:
    """E2E-001: Successful cells export with exit code 0 and match golden HTML.

    Parameters
    ----------
    tmp_path
        Temporary output directory.

    Returns
    -------
    None
        The assertion verifies the E2E golden contract.

    Raises
    ------
    pytest.skip.Exception
        Raised only if the synthetic ui.db builder cannot encode a requested
        cell type; this scenario uses plain SQL cells and runs normally.

    Notes
    -----
    None.
    """
    synthetic_ui_db = _build_synthetic_ui_db(
        tmp_path,
        "successful-cells",
        "nb-e2e-001",
        "ver-e2e-001",
        [{"cell_type": "sql", "sql": "SELECT 1 AS value"}],
    )

    output_path = _run_cli_export(synthetic_ui_db, tmp_path, "successful-cells")
    _assert_matches_golden(output_path, "e2e-001-successful-cells.html")


def test_e2e_002_non_abort_error_matches_golden_html(
    tmp_path: Path,
) -> None:
    """E2E-002: Non-abort error cells continue and match golden HTML.

    Parameters
    ----------
    tmp_path
        Temporary output directory.

    Returns
    -------
    None
        The assertion verifies the E2E golden contract.

    Raises
    ------
    pytest.skip.Exception
        Raised only if the synthetic ui.db builder cannot encode a requested
        cell type; this scenario uses plain SQL cells and runs normally.

    Notes
    -----
    The expected CLI exit code is zero because non-abort errors are rendered
    while later cells continue.
    """
    synthetic_ui_db = _build_synthetic_ui_db(
        tmp_path,
        "non-abort-error",
        "nb-e2e-002",
        "ver-e2e-002",
        [
            {"cell_type": "sql", "sql": "SELECT 1 AS before_error"},
            {"cell_type": "sql", "sql": "SELECT * FROM missing_e2e_table"},
            {"cell_type": "sql", "sql": "SELECT 2 AS after_error"},
        ],
    )

    output_path = _run_cli_export(synthetic_ui_db, tmp_path, "non-abort-error")
    _assert_matches_golden(output_path, "e2e-002-non-abort-error.html")


def test_e2e_003_abort_error_skips_later_cells_and_matches_golden_html(
    tmp_path: Path,
) -> None:
    """E2E-003: Abort errors skip later cells and match golden HTML.

    Parameters
    ----------
    tmp_path
        Temporary output directory.

    Returns
    -------
    None
        The assertion verifies the E2E golden contract.

    Raises
    ------
    pytest.skip.Exception
        Raised only if the synthetic ui.db builder cannot encode a requested
        cell type; this scenario uses plain SQL cells and runs normally.

    Notes
    -----
    The default CLI behavior still exits zero after rendering skipped cells.
    """
    synthetic_ui_db = _build_synthetic_ui_db(
        tmp_path,
        "abort-error",
        "nb-e2e-003",
        "ver-e2e-003",
        [
            {
                "cell_type": "sql",
                "sql": "CREATE TABLE abort_source (id INTEGER PRIMARY KEY)",
            },
            {"cell_type": "sql", "sql": "INSERT INTO abort_source VALUES (1)"},
            {"cell_type": "sql", "sql": "INSERT INTO abort_source VALUES (1)"},
            {"cell_type": "sql", "sql": "SELECT 2 AS skipped_after_abort"},
        ],
    )

    output_path = _run_cli_export(synthetic_ui_db, tmp_path, "abort-error")
    _assert_matches_golden(output_path, "e2e-003-abort-error.html")


def test_e2e_004_large_result_limit_matches_golden_html(
    tmp_path: Path,
) -> None:
    """E2E-004: Large results show the display limit and match golden HTML.

    Parameters
    ----------
    tmp_path
        Temporary output directory.

    Returns
    -------
    None
        The assertion verifies the E2E golden contract.

    Raises
    ------
    pytest.skip.Exception
        Raised only if the synthetic ui.db builder cannot encode a requested
        cell type; this scenario uses plain SQL cells and runs normally.

    Notes
    -----
    The golden output must include the over-1,000-row indication without a total
    count query.
    """
    synthetic_ui_db = _build_synthetic_ui_db(
        tmp_path,
        "large-result-limit",
        "nb-e2e-004",
        "ver-e2e-004",
        [{"cell_type": "sql", "sql": "SELECT * FROM range(1001)"}],
    )

    output_path = _run_cli_export(synthetic_ui_db, tmp_path, "large-result-limit")
    _assert_matches_golden(output_path, "e2e-004-large-result-limit.html")


def test_e2e_005_nested_and_null_values_match_golden_html(
    tmp_path: Path,
) -> None:
    """E2E-005: NULL and nested DuckDB values match golden HTML.

    Parameters
    ----------
    tmp_path
        Temporary output directory.

    Returns
    -------
    None
        The assertion verifies the E2E golden contract.

    Raises
    ------
    pytest.skip.Exception
        Raised only if the synthetic ui.db builder cannot encode a requested
        cell type; this scenario uses plain SQL cells and runs normally.

    Notes
    -----
    The golden file covers NULL plus STRUCT, LIST, and MAP string rendering.
    """
    synthetic_ui_db = _build_synthetic_ui_db(
        tmp_path,
        "nested-and-null-values",
        "nb-e2e-005",
        "ver-e2e-005",
        [
            {
                "cell_type": "sql",
                "sql": (
                    "SELECT NULL AS null_value, "
                    "{'name': 'duckdb', 'count': 2} AS struct_value, "
                    "[1, 2, 3] AS list_value, "
                    "MAP(['a', 'b'], [10, 20]) AS map_value"
                ),
            },
        ],
    )

    output_path = _run_cli_export(synthetic_ui_db, tmp_path, "nested-and-null-values")
    _assert_matches_golden(output_path, "e2e-005-nested-and-null-values.html")


def test_e2e_006_chart_cell_fallback_matches_golden_html(
    tmp_path: Path,
) -> None:
    """E2E-006: Chart cells fall back to tables and match golden HTML.

    Parameters
    ----------
    tmp_path
        Temporary output directory.

    Returns
    -------
    None
        The assertion verifies the E2E golden contract.

    Raises
    ------
    pytest.skip.Exception
        Raised by the synthetic ui.db builder because stored notebook format
        v3 has no representation for ``chart`` cells; this is the one
        scenario in this module that is expected to skip.

    Notes
    -----
    Phase 1 renders chart cells as table output with an unsupported-chart note.
    """
    synthetic_ui_db = _build_synthetic_ui_db(
        tmp_path,
        "chart-cell",
        "nb-e2e-006",
        "ver-e2e-006",
        [
            {
                "cell_type": "chart",
                "sql": "SELECT 'alpha' AS label, 10 AS value",
            },
        ],
    )

    output_path = _run_cli_export(synthetic_ui_db, tmp_path, "chart-cell")
    _assert_matches_golden(output_path, "e2e-006-chart-cell.html")


def test_e2e_007_create_secret_masking_matches_golden_html(
    tmp_path: Path,
) -> None:
    """E2E-007: CREATE SECRET values are masked and match golden HTML.

    Parameters
    ----------
    tmp_path
        Temporary output directory.

    Returns
    -------
    None
        The assertion verifies the E2E golden contract.

    Raises
    ------
    pytest.skip.Exception
        Raised only if the synthetic ui.db builder cannot encode a requested
        cell type; this scenario uses plain SQL cells and runs normally.

    Notes
    -----
    Structural CREATE SECRET elements should remain visible while parameter
    values are replaced with mask text.
    """
    synthetic_ui_db = _build_synthetic_ui_db(
        tmp_path,
        "create-secret",
        "nb-e2e-007",
        "ver-e2e-007",
        [
            {
                "cell_type": "sql",
                "sql": (
                    "CREATE SECRET e2e_secret "
                    "(TYPE S3, KEY_ID 'AKIA_TEST', SECRET 'plain-text-secret')"
                ),
            },
        ],
    )

    output_path = _run_cli_export(synthetic_ui_db, tmp_path, "create-secret")
    _assert_matches_golden(output_path, "e2e-007-create-secret.html")


def test_e2e_008_transaction_statement_cells_match_golden_html(
    tmp_path: Path,
) -> None:
    """E2E-008: Transaction statement cells are rejected and match golden HTML.

    Parameters
    ----------
    tmp_path
        Temporary output directory.

    Returns
    -------
    None
        The assertion verifies the E2E golden contract.

    Raises
    ------
    pytest.skip.Exception
        Raised only if the synthetic ui.db builder cannot encode a requested
        cell type; this scenario uses plain SQL cells and runs normally.

    Notes
    -----
    BEGIN, COMMIT, and ROLLBACK cells are expected to render as rejected rather
    than being executed.
    """
    synthetic_ui_db = _build_synthetic_ui_db(
        tmp_path,
        "transaction-statements",
        "nb-e2e-008",
        "ver-e2e-008",
        [
            {"cell_type": "sql", "sql": "BEGIN"},
            {"cell_type": "sql", "sql": "SELECT 1 AS inside_transaction"},
            {"cell_type": "sql", "sql": "COMMIT"},
        ],
    )

    output_path = _run_cli_export(synthetic_ui_db, tmp_path, "transaction-statements")
    _assert_matches_golden(output_path, "e2e-008-transaction-statements.html")
