"""Shared structlog configuration for DuckDB UI notebook export.

Parameters
----------
None
    This module does not accept parameters.

Returns
-------
None
    Importing this module exposes logging helpers.

Raises
------
None
    Importing this module should not raise package-specific exceptions.

Notes
-----
Importing this module does not configure structlog globally. CLI entry points
must call ``configure_logging`` before emitting diagnostic, warning, or progress
logs. Output is written to stderr through stdlib logging.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_CONFIGURED = False


def configure_logging(level: int | str = logging.INFO) -> None:
    """Configure structlog for CLI diagnostics.

    Parameters
    ----------
    level
        Logging threshold accepted by ``logging.Logger.setLevel``.

    Returns
    -------
    None
        The global structlog and stdlib logging configuration is updated.

    Raises
    ------
    ValueError
        Raised by stdlib logging when ``level`` is invalid.

    Notes
    -----
    The function is idempotent. Repeated calls keep the first configuration and
    avoid adding duplicate stderr handlers.
    """

    global _CONFIGURED

    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer(),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(*args: Any, **initial_values: Any) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger.

    Parameters
    ----------
    *args
        Positional arguments forwarded to ``structlog.get_logger``.
    **initial_values
        Initial bound values forwarded to ``structlog.get_logger``.

    Returns
    -------
    structlog.stdlib.BoundLogger
        Logger instance for diagnostic, warning, and progress messages.

    Raises
    ------
    TypeError
        Raised when arguments are incompatible with ``structlog.get_logger``.

    Notes
    -----
    This helper does not configure logging. Call ``configure_logging`` from the
    CLI entry point before emitting logs.
    """

    return structlog.get_logger(*args, **initial_values)
