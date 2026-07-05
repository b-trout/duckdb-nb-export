"""Synthetic DuckDB UI database builders for tests.

Parameters
----------
None
    This module does not accept parameters.

Returns
-------
None
    Importing this module exposes synthetic fixture helpers.

Raises
------
None
    Importing this module should not raise package-specific exceptions.

Notes
-----
Implementation is blocked by design doc 6.2#1 (notebook JSON schema
investigation).
"""


def build_ui_db(notebooks, dest_dir):
    """Build a synthetic DuckDB UI database fixture.

    Parameters
    ----------
    notebooks : list[dict]
        Notebook specs. Each item has ``"name"`` (str), ``"notebook_id"``
        (str), optional ``"updated_at"`` (ISO8601 string), and ``"versions"``
        (list[dict]). Each version has ``"version_id"`` (str),
        ``"created_at"`` (str), and ``"cells"`` (list[dict]). Each cell has
        ``"cell_type"`` (str) and ``"sql"`` (str).
    dest_dir
        Destination directory where the synthetic ``ui.db`` should be written.

    Returns
    -------
    pathlib.Path
        Path to the generated synthetic ``ui.db``.

    Raises
    ------
    NotImplementedError
        Always raised because this helper is blocked by design doc 6.2#1
        (notebook JSON schema investigation).

    Notes
    -----
    This helper will become usable after the unofficial DuckDB UI notebook JSON
    schema is investigated.
    """
    raise NotImplementedError
