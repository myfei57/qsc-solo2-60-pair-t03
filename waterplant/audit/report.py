"""Audit projections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AuditState:
    """Every audit entry plus the total count."""

    entries: list[dict[str, Any]]
    count: int

    def as_dict(self) -> dict[str, object]:
        return {"entries": self.entries, "count": self.count}
