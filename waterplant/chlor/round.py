"""Persisted chlorine control rounds and feedback recovery state."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from waterplant.store.store import Store

from .report import DosePlan, DoseStrategy, FeedbackResult, RoundStatus

ROUND_KEY = "chlor:round"
HISTORY_KEY = "chlor:rounds"
DEFAULT_CLOCK = 0.0


@dataclass(frozen=True)
class RoundState:
    """The single round whose feedback gate is currently open."""

    round_id: str
    request_id: str
    sequence: int
    started_at: float
    due_at: float
    contact_minutes: float
    target: float
    ammonia_n: float
    previous_ammonia_n: float | None
    plan: dict[str, Any]
    status: RoundStatus = RoundStatus.FEEDFORWARD
    feedback: dict[str, Any] | None = None
    lost_at: float | None = None
    loss_id: str = ""
    recovery_id: str = ""
    reading_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RoundState":
        return cls(
            round_id=str(payload["round_id"]),
            request_id=str(payload.get("request_id", "")),
            sequence=int(payload.get("sequence", 0)),
            started_at=float(payload.get("started_at", 0.0)),
            due_at=float(payload.get("due_at", 0.0)),
            contact_minutes=float(payload.get("contact_minutes", 0.0)),
            target=float(payload.get("target", 0.0)),
            ammonia_n=float(payload.get("ammonia_n", 0.0)),
            previous_ammonia_n=(
                None
                if payload.get("previous_ammonia_n") is None
                else float(payload["previous_ammonia_n"])
            ),
            plan=dict(payload.get("plan", {})),
            status=RoundStatus(str(payload.get("status", RoundStatus.FEEDFORWARD))),
            feedback=(
                None if payload.get("feedback") is None else dict(payload["feedback"])
            ),
            lost_at=(
                None if payload.get("lost_at") is None else float(payload["lost_at"])
            ),
            loss_id=str(payload.get("loss_id", "")),
            recovery_id=str(payload.get("recovery_id", "")),
            reading_id=str(payload.get("reading_id", "")),
        )

    def replace(self, **changes: Any) -> "RoundState":
        data = self.as_dict()
        data.update(changes)
        return RoundState.from_dict(data)

    def dose_plan(self) -> DosePlan:
        data = dict(self.plan)
        strategy = data.pop("strategy")
        return DosePlan(strategy=DoseStrategy(strategy), **data)

    def feedback_result(self) -> FeedbackResult | None:
        if self.feedback is None:
            return None
        return FeedbackResult(**self.feedback)


@dataclass(frozen=True)
class RoundLedger:
    """Round projection saved in the atomic key/value store."""

    current: RoundState | None
    count: int
    rounds: list[dict[str, Any]] = field(default_factory=list)


class RoundRepository:
    """Loads and saves chlorine rounds without hiding partial failure state."""

    def __init__(self, store: Store, clock: Callable[[], float] | None = None) -> None:
        self._store = store
        self._clock = clock

    def _now(self) -> float:
        return self._clock() if self._clock is not None else DEFAULT_CLOCK

    def current(self) -> RoundState | None:
        raw, present = self._store.get(ROUND_KEY)
        if not present:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        return RoundState.from_dict(payload)

    def save(self, round_state: RoundState) -> None:
        self._store.put(ROUND_KEY, json.dumps(round_state.as_dict(), ensure_ascii=False))

    def clear(self) -> None:
        self._store.delete(ROUND_KEY)

    def list_rounds(self) -> list[dict[str, Any]]:
        raw, present = self._store.get(HISTORY_KEY)
        if not present:
            return []
        try:
            payload = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(payload, list):
            return []
        return [dict(item) for item in payload if isinstance(item, dict)]

    def find(self, round_id: str = "", recovery_id: str = "", reading_id: str = "") -> RoundState | None:
        candidates: list[dict[str, Any]] = []
        current = self.current()
        if current is not None:
            candidates.append(current.as_dict())
        candidates.extend(reversed(self.list_rounds()))
        for item in candidates:
            if round_id and item.get("round_id") == round_id:
                return RoundState.from_dict(item)
            if recovery_id and item.get("recovery_id") == recovery_id:
                return RoundState.from_dict(item)
            if reading_id and item.get("reading_id") == reading_id:
                return RoundState.from_dict(item)
        return None

    def latest(self) -> RoundState | None:
        rounds = self.list_rounds()
        if not rounds:
            return None
        return RoundState.from_dict(rounds[-1])

    def next_sequence(self) -> int:
        current = self.current()
        if current is not None:
            return current.sequence + 1
        return len(self.list_rounds()) + 1

    def start(self, plan: DosePlan, request_id: str, round_id: str = "") -> RoundState:
        now = self._now()
        return RoundState(
            round_id=round_id or f"chlor-{uuid.uuid4().hex}",
            request_id=request_id or f"req-{uuid.uuid4().hex}",
            sequence=self.next_sequence(),
            started_at=now,
            due_at=now + plan.feedback_minutes * 60.0,
            contact_minutes=plan.contact_minutes,
            target=plan.target,
            ammonia_n=plan.ammonia_n,
            previous_ammonia_n=(
                None if plan.first_round else plan.previous_ammonia_n
            ),
            plan=plan.as_dict(),
        )

    def finish(self, round_state: RoundState) -> None:
        rounds = self.list_rounds()
        rounds.append(round_state.as_dict())
        self._store.put(HISTORY_KEY, json.dumps(rounds, ensure_ascii=False))
        self.clear()

    def ledger(self) -> RoundLedger:
        return RoundLedger(current=self.current(), count=len(self.list_rounds()), rounds=self.list_rounds())
