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
import os
import sys
from typing import Any

import structlog

_CONFIGURED = False


def _should_use_colors() -> bool:
    """Return whether ANSI color codes should be emitted on stderr.

    Returns
    -------
    bool
        True when stderr is a tty and the ``NO_COLOR`` environment variable
        is not set; False otherwise.

    Notes
    -----
    Per the `NO_COLOR <https://no-color.org/>`_ convention, the mere
    presence of the ``NO_COLOR`` environment variable disables color output,
    regardless of its value (including an empty string).
    """

    if "NO_COLOR" in os.environ:
        return False
    return sys.stderr.isatty()


def configure_logging(level: int | str = logging.INFO, *, force: bool = False) -> None:
    """Configure structlog for CLI diagnostics.

    Parameters
    ----------
    level
        Logging threshold accepted by ``logging.Logger.setLevel``.
    force
        When True, re-apply the configuration (including ``level`` and the
        color decision) even if ``configure_logging`` already ran. When
        False (the default), a prior call wins and this call is a no-op,
        preserving the previous idempotent behavior for library-ish callers
        that configure logging defensively before use.

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
    Without ``force``, the function is idempotent: repeated calls keep the
    first configuration and avoid adding duplicate stderr handlers. Pass
    ``force=True`` (as the CLI entry point does once it has parsed
    ``-q``/``-v``) to let a later, more specific call win over an earlier
    default call. Loggers are deliberately not cached on first use
    (``cache_logger_on_first_use=False``): a cached logger would keep the
    level and processors it was first used with, so a later forced
    reconfiguration (or ``structlog.testing.capture_logs``) would not
    affect module-level loggers that already emitted an event.
    """

    global _CONFIGURED

    if _CONFIGURED and not force:
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
        structlog.dev.ConsoleRenderer(colors=_should_use_colors()),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
    _CONFIGURED = True


def reset_for_testing() -> None:
    """Reset the module's cached configuration flag for test isolation.

    Returns
    -------
    None
        The ``_CONFIGURED`` flag is cleared so the next ``configure_logging``
        call re-applies the full configuration.

    Notes
    -----
    Intended for use by test fixtures only. This does not reset structlog's
    own global configuration; callers typically also call
    ``structlog.reset_defaults()``.
    """

    global _CONFIGURED
    _CONFIGURED = False


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
