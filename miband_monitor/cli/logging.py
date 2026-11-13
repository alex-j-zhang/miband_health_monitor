"""Logging setup shared by the command-line entry points.

Besides formatting, this module installs a redacting formatter so a local .env
device address or an accidental long hex blob never appears verbatim in console
logs, tracebacks or issue reports, without mutating shared LogRecord objects
seen by other handlers (such as pytest's caplog).
"""

from __future__ import annotations

import copy
import logging
import sys

from ..config.defaults import DEFAULT_LOG_LEVEL
from ..config.privacy import redact_text

#: Historical format used by every log line of the project.
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


class RedactingFormatter(logging.Formatter):
    """Format log records (including tracebacks) with MAC/secret redaction."""

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return redact_text(rendered)


class RedactionFilter(logging.Filter):
    """Optional filter that redacts a shallow copy's message when attached directly."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401 - stdlib signature
        return True

    def redact_record(self, record: logging.LogRecord) -> logging.LogRecord:
        """Return a redacted shallow copy without mutating the caller's record."""
        cloned = copy.copy(record)
        try:
            message = cloned.getMessage()
        except Exception:  # pragma: no cover - defensive
            return cloned
        cloned.msg = redact_text(message)
        cloned.args = ()
        return cloned


def configure_logging(level_name: str) -> None:
    """Configure root logging, tolerating legacy console encodings on Windows.

    When stdout/stderr are redirected to a file the console encoding may be a
    legacy code page (cp1252/cp936) that cannot encode every log message. We
    replace instead of raising so a redirected run never aborts just because of
    decoration characters.
    """
    level = getattr(logging, (level_name or DEFAULT_LOG_LEVEL).upper(), logging.INFO)
    logging.basicConfig(level=level, format=LOG_FORMAT)
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in root_logger.handlers:
        if not isinstance(handler.formatter, RedactingFormatter):
            fmt = getattr(handler.formatter, "_fmt", None) or LOG_FORMAT
            datefmt = getattr(handler.formatter, "datefmt", None)
            handler.setFormatter(RedactingFormatter(fmt=fmt, datefmt=datefmt))
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):  # pragma: no cover - defensive
                pass


# Backwards-compatible private name used by the old module.
_configure_logging = configure_logging
