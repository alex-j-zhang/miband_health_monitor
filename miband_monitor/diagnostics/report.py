"""Result collection for the read-only environment doctor.

The doctor prints one aligned line per check and a final summary; the exact
prefixes, widths and severity markers are part of the command's stable output
(users and issue reports quote them), so they live here unchanged.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger("MiBandDoctor")

OK = "OK"
INFO = "INFO"
WARN = "WARN"
FAIL = "FAIL"

_PREFIX = {OK: "[OK]  ", INFO: "[INFO]", WARN: "[WARN]", FAIL: "[FAIL]"}
_MARK = {OK: "ok", INFO: "info", WARN: "warning", FAIL: "error"}


class _Report:
    """Collect and print check results."""

    def __init__(self, emit: Optional[Callable[[str], None]] = None) -> None:
        self._lines: list[str] = []
        self._emit = emit or print
        self.counts = {OK: 0, INFO: 0, WARN: 0, FAIL: 0}

    def add(self, status: str, label: str, detail: str, hint: Optional[str] = None) -> None:
        self.counts[status] = self.counts.get(status, 0) + 1
        line = f"{_PREFIX[status]} {label:<14} {detail}"
        self._lines.append(line)
        if hint:
            for part in hint.splitlines():
                self._lines.append(f"       {'':<14} {part}")

    def flush(self) -> None:
        for line in self._lines:
            self._emit(line)

    def summary(self) -> str:
        return (
            f"{self.counts[FAIL]} error(s), {self.counts[WARN]} warning(s), "
            f"{self.counts[OK]} check(s) passed"
        )
