"""Exceptions and result types shared by the history fetch/parse/sync layers.

Keeping these in one dependency-free module lets the protocol codecs raise the
same exceptions the fetcher and the high-level sync loop report, without any
circular imports between those modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class HistoryFetchError(RuntimeError):
    """Raised when the band-side history fetch fails or is out of sync."""


class HistoryParseError(ValueError):
    """Raised when a fetched payload cannot be decoded."""


@dataclass
class HistorySyncResult:
    """Summary of one history synchronisation run."""

    spo2: list[dict] = field(default_factory=list)
    activity: list[dict] = field(default_factory=list)
    #: Opt-in stream with its own table (layout verified, field semantics unknown).
    spo2_sleep: list[dict] = field(default_factory=list)
    rounds: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def total_samples(self) -> int:
        return len(self.spo2) + len(self.activity) + len(self.spo2_sleep)
