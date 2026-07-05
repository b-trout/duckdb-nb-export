"""DuckDB UI stored notebook models.

Parameters
----------
None
    This module does not accept parameters.

Returns
-------
None
    Importing this module exposes stored JSON model classes.

Raises
------
None
    Importing this module should not raise package-specific exceptions.

Notes
-----
``Stored*`` models represent DuckDB UI notebook JSON v3 stored in ``ui.db``.
"""

from pydantic import BaseModel, ConfigDict, Field


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
