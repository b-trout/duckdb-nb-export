"""HTML rendering API for DuckDB UI notebook export.

Parameters
----------
None
    This module does not accept parameters.

Returns
-------
None
    Importing this module exposes rendering models and functions.

Raises
------
None
    Importing this module should not raise package-specific exceptions.

Notes
-----
Functions are intentionally unimplemented stubs for test-first development.
"""

from dataclasses import dataclass

from duckdb_ui_notebook_export.executor import ExecutionReport
from duckdb_ui_notebook_export.models import Notebook


@dataclass
class ExportMetadata:
    """Metadata embedded in exported HTML.

    Parameters
    ----------
    exported_at_utc
        UTC export timestamp as an ISO 8601 string.
    duckdb_version
        DuckDB version used for execution.
    notebook_version_id
        Selected notebook version identifier.
    tool_version
        Export tool version.
    warnings
        Warning messages to include in metadata.

    Returns
    -------
    ExportMetadata
        Dataclass instance containing export metadata.

    Raises
    ------
    TypeError
        Raised by dataclass construction when required arguments are missing.

    Notes
    -----
    Metadata values are intentionally plain strings for stable rendering.
    """

    exported_at_utc: str
    duckdb_version: str
    notebook_version_id: str
    tool_version: str
    warnings: list[str]


def mask_secrets(sql: str) -> str:
    """Mask values in DuckDB CREATE SECRET statements.

    Parameters
    ----------
    sql
        SQL text to sanitize before rendering.

    Returns
    -------
    str
        SQL text with CREATE SECRET parameter values replaced by ``***``.

    Raises
    ------
    NotImplementedError
        Always raised until implementation is driven by tests.

    Notes
    -----
    Structural elements such as TYPE, PROVIDER, and SCOPE must remain visible.
    """
    raise NotImplementedError


def truncate_value(value: str, limit: int = 500) -> str:
    """Truncate a rendered scalar value for HTML display.

    Parameters
    ----------
    value
        String value to truncate.
    limit
        Maximum number of characters to preserve before adding an annotation.

    Returns
    -------
    str
        Original or truncated value suitable for rendering.

    Raises
    ------
    NotImplementedError
        Always raised until implementation is driven by tests.

    Notes
    -----
    The intended implementation records the original length when truncating.
    """
    raise NotImplementedError


def render_html(
    notebook: Notebook,
    report: ExecutionReport,
    metadata: ExportMetadata,
) -> str:
    """Render a notebook execution report as a single static HTML document.

    Parameters
    ----------
    notebook
        Notebook definition to render.
    report
        Execution results for the notebook cells.
    metadata
        Export metadata for the document.

    Returns
    -------
    str
        Complete HTML document.

    Raises
    ------
    NotImplementedError
        Always raised until implementation is driven by tests.

    Notes
    -----
    The intended implementation uses Jinja2 autoescaping and inline assets.
    """
    raise NotImplementedError
