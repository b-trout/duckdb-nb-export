"""Pydantic models for notebook metadata and content.

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
``Stored*`` models represent DuckDB UI JSON v3 stored in ``ui.db``.
``Notebook`` and ``Cell`` represent this tool's internal notebook format.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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


class StoredCell(BaseModel):
    """A notebook cell in DuckDB UI stored JSON v3.

    Parameters
    ----------
    query
        SQL text stored by DuckDB UI.
    cell_id
        Numeric cell identifier, stored as ``cellId`` in JSON.
    use_database
        Optional database name, stored as ``useDatabase`` in JSON.
    is_active
        Optional active-cell flag, stored as ``isActive`` in JSON.
    run_mode
        Optional execution mode, stored as ``runMode`` in JSON.

    Returns
    -------
    StoredCell
        Validated stored cell data.

    Raises
    ------
    pydantic.ValidationError
        Raised when required fields cannot be validated.

    Notes
    -----
    Unknown fields are preserved to detect only incompatible schema changes.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    query: str | None = None
    cell_id: int = Field(alias="cellId")
    use_database: str | None = Field(default=None, alias="useDatabase")
    is_active: bool | None = Field(default=None, alias="isActive")
    run_mode: str | None = Field(default=None, alias="runMode")


class StoredNotebook(BaseModel):
    """A notebook document in DuckDB UI stored JSON v3.

    Parameters
    ----------
    notebook_serialization_format
        Stored serialization format, stored as ``notebookSerializationFormat``
        in JSON.
    cells
        Ordered stored cells.
    current_database
        Optional current database name, stored as ``currentDatabase`` in JSON.
    view_mode
        Stored view-mode payload, stored as ``viewMode`` in JSON.
    version
        Stored notebook version number.

    Returns
    -------
    StoredNotebook
        Validated stored notebook data.

    Raises
    ------
    pydantic.ValidationError
        Raised when required fields cannot be validated.

    Notes
    -----
    This model mirrors the ``notebook_versions.json`` payload, not this tool's
    internal export model.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    notebook_serialization_format: int = Field(alias="notebookSerializationFormat")
    cells: list[StoredCell]
    current_database: str | None = Field(default=None, alias="currentDatabase")
    view_mode: dict = Field(alias="viewMode")
    version: int


class Cell(BaseModel):
    """A notebook cell to execute and render.

    Parameters
    ----------
    cell_type
        Cell kind. SQL is the default Phase 1 cell type.
    sql
        SQL text for the cell.

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
