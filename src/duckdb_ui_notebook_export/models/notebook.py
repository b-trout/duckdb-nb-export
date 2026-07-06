"""Internal notebook models for DuckDB UI notebook export.

Parameters
----------
None
    This module does not accept parameters.

Returns
-------
None
    Importing this module exposes model classes.

Raises
------
None
    Importing this module should not raise package-specific exceptions.

Notes
-----
All models allow extra fields to tolerate the unofficial DuckDB UI schema.
``Notebook`` and ``Cell`` represent this tool's internal notebook format.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class NotebookInfo(BaseModel):
    """Metadata for a notebook stored by DuckDB UI.

    Parameters
    ----------
    name
        Human-readable notebook name.
    notebook_id
        Internal notebook identifier.
    updated_at
        Last update timestamp.

    Returns
    -------
    NotebookInfo
        Validated notebook metadata.

    Raises
    ------
    pydantic.ValidationError
        Raised when required fields cannot be validated.

    Notes
    -----
    Unknown fields are preserved for forward compatibility.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    notebook_id: str
    updated_at: datetime


class VersionInfo(BaseModel):
    """Metadata for a notebook version stored by DuckDB UI.

    Parameters
    ----------
    version_id
        Internal notebook version identifier.
    created_at
        Version creation timestamp.

    Returns
    -------
    VersionInfo
        Validated notebook version metadata.

    Raises
    ------
    pydantic.ValidationError
        Raised when required fields cannot be validated.

    Notes
    -----
    Unknown fields are preserved for forward compatibility.
    """

    model_config = ConfigDict(extra="allow")

    version_id: str
    created_at: datetime


class Cell(BaseModel):
    """A notebook cell to execute and render.

    Parameters
    ----------
    cell_type
        Cell kind. SQL is the default Phase 1 cell type.
    sql
        SQL text for the cell.
    use_database
        Optional database name this cell selects in DuckDB UI. The executor
        issues a best-effort ``USE`` before running the cell (ADR-008).

    Returns
    -------
    Cell
        Validated notebook cell.

    Raises
    ------
    pydantic.ValidationError
        Raised when required fields cannot be validated.

    Notes
    -----
    Unknown fields are preserved for chart and future cell metadata.
    """

    model_config = ConfigDict(extra="allow")

    cell_type: str = "sql"
    sql: str
    use_database: str | None = None


class Notebook(BaseModel):
    """Notebook content selected for export.

    Parameters
    ----------
    name
        Human-readable notebook name.
    version_id
        Selected notebook version identifier.
    cells
        Ordered cells in the notebook.
    database_info
        Optional connection metadata from notebook JSON.

    Returns
    -------
    Notebook
        Validated notebook content.

    Raises
    ------
    pydantic.ValidationError
        Raised when required fields cannot be validated.

    Notes
    -----
    ``database_info`` remains free-form until design-doc item 6.2#1 is done.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    version_id: str
    cells: list[Cell]
    database_info: dict[str, Any] | None = None
