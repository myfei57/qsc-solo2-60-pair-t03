"""Append only audit entries for every chemical dose."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from waterplant.store.commands import append_command, load_commands
from waterplant.store.store import Store

from .report import AuditState

AUDIT_KEY = "audit:entries"


@dataclass(frozen=True)
class Entry:
    """One recorded dosing decision."""

    id: str
    time: str
    kind: str
    detail: str
    phase: str = ""
    round_id: str = ""
    amount: float | None = None
    payload: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = self.payload or {}
        data = {
            "id": self.id,
            "time": self.time,
            "kind": self.kind,
            "detail": self.detail,
            "phase": self.phase,
            "round_id": self.round_id,
            "amount": self.amount,
            "payload": payload,
        }
        return data


class Auditor:
    """Records and queries dosing entries."""

    def __init__(self, store: Store, clock: Callable[[], float] | None = None) -> None:
        self._store = store
        self._clock = clock or time.time

    def record(self, kind: str, detail: str) -> Entry:
        return self.record_event(kind=kind, detail=detail)

    def record_event(
        self,
        *,
        kind: str,
        detail: str = "",
        amount: float | None = None,
        phase: str = "",
        round_id: str = "",
        entry_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> Entry:
        entry = Entry(
            id=entry_id or uuid.uuid4().hex,
            time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock())),
            kind=kind,
            detail=detail,
            phase=phase,
            round_id=round_id,
            amount=amount,
            payload=dict(payload or {}),
        )
        append_command(self._store, AUDIT_KEY, json.dumps(entry.as_dict(), ensure_ascii=False))
        return entry

    def record_once(self, entry_id: str, **values: Any) -> Entry:
        """Record an event with a caller supplied id, or return the existing one."""

        if not entry_id:
            raise ValueError("idempotent audit entry requires an id")
        with self._store.mutate():
            existing = self.find(entry_id)
            if existing is not None:
                return existing
            return self.record_event(entry_id=entry_id, **values)

    def find(self, entry_id: str) -> Entry | None:
        for entry in self.entries():
            if entry.id == entry_id:
                return entry
        return None

    def entries(self) -> list[Entry]:
        entries: list[Entry] = []
        for item in load_commands(self._store, AUDIT_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            raw_extra = payload.get("payload", {})
            extra = raw_extra if isinstance(raw_extra, dict) else {}
            raw_amount = payload.get("amount")
            amount = None if raw_amount is None else _safe_float(raw_amount)
            entries.append(
                Entry(
                    id=str(payload.get("id", "")),
                    time=str(payload.get("time", "")),
                    kind=str(payload.get("kind", "")),
                    detail=str(payload.get("detail", "")),
                    phase=str(payload.get("phase", "")),
                    round_id=str(payload.get("round_id", "")),
                    amount=amount,
                    payload=dict(extra),
                )
            )
        return entries

    def last(self) -> Entry | None:
        entries = self.entries()
        if not entries:
            return None
        return entries[-1]

    def count(self) -> int:
        return len(self.entries())

    def count_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries():
            counts[entry.kind] = counts.get(entry.kind, 0) + 1
        return counts

    def filter(self, kind: str) -> list[Entry]:
        return [entry for entry in self.entries() if entry.kind == kind]

    def kinds(self) -> list[str]:
        seen: list[str] = []
        for entry in self.entries():
            if entry.kind not in seen:
                seen.append(entry.kind)
        return seen

    def totals(self) -> dict[str, float]:
        """Sum the numeric dose recorded for each chemical."""

        totals: dict[str, float] = {}
        for entry in self.entries():
            amount = entry.amount
            if amount is None:
                try:
                    amount = float(entry.detail)
                except ValueError:
                    continue
            totals[entry.kind] = totals.get(entry.kind, 0.0) + amount
        return totals

    def state(self) -> AuditState:
        entries = self.entries()
        return AuditState(entries=[entry.as_dict() for entry in entries], count=len(entries))

    def describe(self) -> str:
        return f"audit entries={len(self.entries())}"


def _safe_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
