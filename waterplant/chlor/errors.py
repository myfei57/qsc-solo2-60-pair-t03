"""Domain conflicts that the console reports as 409 rather than bad input."""

from __future__ import annotations


class ChlorRoundConflict(RuntimeError):
    """A round is in the wrong lifecycle state for the requested operation."""
