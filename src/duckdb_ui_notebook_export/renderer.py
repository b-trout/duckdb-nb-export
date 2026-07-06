"""HTML rendering API for DuckDB UI notebook export."""

import html
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

import duckdb
from jinja2 import Environment
from markupsafe import Markup
from pygments import highlight
from pygments.formatters.html import HtmlFormatter
from pygments.lexers.sql import SqlLexer

from duckdb_ui_notebook_export.executor import CellResult, CellStatus, ExecutionReport
from duckdb_ui_notebook_export.models import Notebook

_SECRET_STRUCTURAL_KEYS = frozenset({"TYPE", "PROVIDER", "SCOPE"})
_CREATE_SECRET_RE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?SECRET\b", re.IGNORECASE
)
_PARAMETER_KEY_RE = re.compile(r"\A\s*([A-Za-z_][A-Za-z0-9_]*)\b")
_SQL_LEXER = SqlLexer()
_SQL_FORMATTER = HtmlFormatter(noclasses=False)
_PYGMENTS_CSS = _SQL_FORMATTER.get_style_defs(".highlight")
if "url(" in _PYGMENTS_CSS.lower():
    msg = "Pygments CSS unexpectedly contains external resource syntax."
    raise RuntimeError(msg)

_BASE_CSS = """
:root {
  color-scheme: light dark;
  --bg: #f8fafc;
  --fg: #111827;
  --muted: #4b5563;
  --border: #d1d5db;
  --panel: #ffffff;
  --code-bg: #f3f4f6;
  --accent: #0f766e;
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111827;
    --fg: #f9fafb;
    --muted: #d1d5db;
    --border: #4b5563;
    --panel: #1f2937;
    --code-bg: #0f172a;
    --accent: #5eead4;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  line-height: 1.5;
}
main, footer {
  max-width: 1120px;
  margin: 0 auto;
  padding: 24px;
}
header {
  border-bottom: 1px solid var(--border);
  background: var(--panel);
}
header .inner {
  max-width: 1120px;
  margin: 0 auto;
  padding: 24px;
}
h1 { margin: 0; font-size: 1.6rem; }
h2 { margin: 0 0 12px; font-size: 1.05rem; }
.cell {
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel);
  margin: 0 0 20px;
  overflow: hidden;
}
.cell-body { padding: 16px; }
.sql {
  background: var(--code-bg);
  border-bottom: 1px solid var(--border);
  overflow-x: auto;
}
.sql pre { margin: 0; padding: 16px; }
.note, .status, footer {
  color: var(--muted);
}
.status {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px 12px;
}
table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
}
th, td {
  border: 1px solid var(--border);
  padding: 8px 10px;
  text-align: left;
  vertical-align: top;
  overflow-wrap: anywhere;
}
th { background: var(--code-bg); }
.warnings {
  border-top: 1px solid var(--border);
  margin-top: 12px;
  padding-top: 12px;
}
a { color: var(--accent); }
"""

_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ notebook.name }}</title>
  <style>
{{ css }}
{{ pygments_css }}
  </style>
</head>
<body>
  <header>
    <div class="inner">
      <h1>{{ notebook.name }}</h1>
    </div>
  </header>
  <main>
    {% for cell in cells %}
    <section class="cell">
      <div class="sql">{{ cell.sql_html }}</div>
      <div class="cell-body">
        <h2>Cell {{ loop.index }}</h2>
        {% if cell.chart_note %}
        <p class="note">{{ cell.chart_note }}</p>
        {% endif %}
        {% if cell.columns %}
        <table>
          <thead>
            <tr>{% for column in cell.columns %}<th>{{ column }}</th>{% endfor %}</tr>
          </thead>
          <tbody>
            {% for row in cell.rows %}
            <tr>{% for value in row %}<td>{{ value }}</td>{% endfor %}</tr>
            {% endfor %}
          </tbody>
        </table>
        {% endif %}
        {% if cell.status_message %}
        <p class="status">{{ cell.status_message }}</p>
        {% endif %}
        {% if cell.truncation_note %}
        <p class="note">{{ cell.truncation_note }}</p>
        {% endif %}
      </div>
    </section>
    {% endfor %}
  </main>
  <footer>
    <div>Exported at {{ metadata.exported_at_utc }} UTC</div>
    <div>DuckDB {{ metadata.duckdb_version }}</div>
    <div>Notebook version {{ metadata.notebook_version_id }}</div>
    <div>Tool version {{ metadata.tool_version }}</div>
    {% if metadata.warnings %}
    <div class="warnings">
      <strong>Warnings</strong>
      <ul>{% for warning in metadata.warnings %}<li>{{ warning }}</li>{% endfor %}</ul>
    </div>
    {% endif %}
  </footer>
</body>
</html>
"""

_ENVIRONMENT = Environment(autoescape=True)
_HTML_TEMPLATE = _ENVIRONMENT.from_string(_TEMPLATE)


@dataclass(frozen=True)
class _RenderedCell:
    sql_html: Markup
    columns: list[str]
    rows: Iterable[tuple[Markup, ...]]
    status_message: str | None
    truncation_note: str | None
    chart_note: str | None


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
    None
        This function does not raise package-specific exceptions.

    Notes
    -----
    Structural elements such as TYPE, PROVIDER, and SCOPE must remain
    visible. Masking is scoped per statement (via
    ``duckdb.extract_statements``) rather than to the whole cell text, since
    a cell text spanning multiple statements would otherwise have its
    parameter region span from the first ``(`` to the last ``)`` in the
    entire cell, corrupting or dropping every statement after the CREATE
    SECRET call (issue #29). When the cell contains exactly one statement,
    the original text is masked directly and returned byte-for-byte aside
    from the parameter substitution, matching pre-#29 behavior exactly.
    """
    if not _CREATE_SECRET_RE.search(sql):
        return sql

    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error:
        return _mask_secret_parameters_in_text(sql)

    if len(statements) <= 1:
        return _mask_secret_parameters_in_text(sql)

    return ";\n".join(
        _mask_secret_parameters_in_text(statement.query)
        if _CREATE_SECRET_RE.search(statement.query)
        else statement.query
        for statement in statements
    )


def _mask_secret_parameters_in_text(sql: str) -> str:
    """Mask CREATE SECRET parameter values within a single statement's text."""
    start = sql.find("(")
    end = sql.rfind(")")
    if start == -1 or end == -1 or end <= start:
        return sql

    masked_parameters = ", ".join(
        _mask_secret_parameter(parameter)
        for parameter in _split_secret_parameters(sql[start + 1 : end])
    )
    return f"{sql[: start + 1]}{masked_parameters}{sql[end:]}"


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
    None
        This function does not raise package-specific exceptions.

    Notes
    -----
    The original length is recorded when truncating.
    """
    if len(value) <= limit:
        return value
    return f"{value[:limit]} (truncated, {len(value)} characters total)"


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
        Complete HTML document with no trailing whitespace on any line and a
        single trailing newline.

    Raises
    ------
    None
        This function does not raise package-specific exceptions.

    Notes
    -----
    Jinja2 autoescaping is enabled; only Pygments output is marked safe.
    """
    rendered_cells = list(_iter_rendered_cells(notebook, report))
    html_document = _HTML_TEMPLATE.render(
        notebook=notebook,
        cells=rendered_cells,
        metadata=metadata,
        css=_BASE_CSS,
        pygments_css=_PYGMENTS_CSS,
    )
    return _normalize_rendered_html(html_document)


def _normalize_rendered_html(html_document: str) -> str:
    end = len(html_document)
    while end > 0 and html_document[end - 1].isspace():
        end -= 1

    return "".join(_iter_normalized_lines(html_document, end)) + "\n"


def _iter_normalized_lines(html_document: str, end: int) -> Iterator[str]:
    start = 0
    while start < end:
        newline_index = html_document.find("\n", start, end)
        if newline_index == -1:
            yield html_document[start:end].rstrip()
            return

        yield html_document[start:newline_index].rstrip()
        yield "\n"
        start = newline_index + 1


def _split_secret_parameters(parameters: str) -> Iterator[str]:
    quote: str | None = None
    start = 0
    index = 0
    while index < len(parameters):
        char = parameters[index]
        if quote is not None:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == ",":
            yield parameters[start:index].strip()
            start = index + 1
        index += 1
    final = parameters[start:].strip()
    if final:
        yield final


def _mask_secret_parameter(parameter: str) -> str:
    key_match = _PARAMETER_KEY_RE.match(parameter)
    if key_match is None:
        return parameter

    key = key_match.group(1)
    if key.upper() in _SECRET_STRUCTURAL_KEYS:
        return parameter
    return f"{key} ***"


def _iter_rendered_cells(
    notebook: Notebook, report: ExecutionReport
) -> Iterator[_RenderedCell]:
    result_count = len(report.cell_results)
    for index, cell in enumerate(notebook.cells):
        if index < result_count:
            yield _render_cell(cell.sql, cell.cell_type, report.cell_results[index])
        else:
            yield _render_unexecuted_cell(cell.sql)


def _render_unexecuted_cell(sql: str) -> _RenderedCell:
    return _RenderedCell(
        sql_html=_highlight_sql(sql),
        columns=[],
        rows=(),
        status_message="Not executed",
        truncation_note=None,
        chart_note=None,
    )


def _render_cell(sql: str, cell_type: str, result: CellResult) -> _RenderedCell:
    status_message = _status_message(result)
    chart_note = (
        "Chart rendering is not supported: DuckDB UI does not persist chart "
        "configuration, so there is no stored chart to reproduce; results "
        "are shown as a table."
        if cell_type != "sql"
        else None
    )
    return _RenderedCell(
        sql_html=_highlight_sql(mask_secrets(sql)),
        columns=result.columns,
        rows=_iter_formatted_rows(result.rows),
        status_message=status_message,
        truncation_note=_truncation_note(result),
        chart_note=chart_note,
    )


def _highlight_sql(sql: str) -> Markup:
    return Markup(highlight(sql, _SQL_LEXER, _SQL_FORMATTER))  # noqa: S704


def _iter_formatted_rows(
    rows: Iterable[tuple[Any, ...]],
) -> Iterator[tuple[Markup, ...]]:
    for row in rows:
        yield tuple(_format_value(value) for value in row)


def _format_value(value: Any) -> Markup:
    if value is None:
        return Markup("NULL")
    if isinstance(value, bytes):
        return Markup(f"{len(value)} bytes")  # noqa: S704
    return Markup(html.escape(truncate_value(str(value)), quote=False))  # noqa: S704


def _status_message(result: CellResult) -> str | None:
    if result.status is CellStatus.OK:
        if result.affected_rows is not None:
            return f"{result.affected_rows} affected row(s)"
        if not result.columns:
            return "OK"
        return None
    if result.status is CellStatus.SKIPPED_ABORT:
        return "Skipped because the transaction was aborted"
    if result.status is CellStatus.TIMEOUT:
        return _message_with_detail("Timed out while executing the cell", result)
    if result.status is CellStatus.ERROR:
        return _message_with_detail("Error while executing the cell", result)
    if result.status is CellStatus.REJECTED_TRANSACTION_STATEMENT:
        return _message_with_detail(
            "Rejected because transaction control statements are not supported",
            result,
        )
    return _message_with_detail(
        f"Cell finished with status {result.status.value}", result
    )


def _message_with_detail(prefix: str, result: CellResult) -> str:
    if result.error_message:
        return f"{prefix}: {result.error_message}"
    return prefix


def _truncation_note(result: CellResult) -> str | None:
    if not result.truncated:
        return None
    row_count = len(result.rows)
    return (
        f"Showing the first {row_count:,} rows; there were more than "
        f"{row_count:,} rows and the total row count was not computed."
    )
