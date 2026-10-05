"""Feedforward/feedback chlorine dosing controller."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from typing import Any

from waterplant.audit import AUDIT_KEY, Auditor
from waterplant.clearwell import Well
from waterplant.intake import FlowRepository
from waterplant.store.commands import load_commands
from waterplant.store.store import Store

from .report import (
    ChlorConfig,
    ChlorinationState,
    DoseAction,
    DosingRound,
    ResidualObservation,
    StatusEvent,
    contact_time_minutes,
    dose_rate_kg_h,
    model_demand_mg_l,
    select_water_quality,
    validate_ammonia,
    validate_fraction,
    validate_positive,
    validate_residual,
)

CONFIG_KEY = "chlor:config"
STATE_KEY = "chlor:controller"
FEEDFORWARD_KIND = "chlorine-feedforward"
FEEDBACK_KIND = "chlorine-feedback"
MISSING_KIND = "chlorine-residual-missing"
RECOVERY_KIND = "chlorine-residual-recovered"
MAX_HISTORY = 50
MAX_PROCESSED_REQUESTS = 200


class ChlorinationBusyError(RuntimeError):
    """Raised when timing or round state forbids the requested transition."""


class ChlorinationController:
    """Coordinate feedforward dosing, delayed residuals and feedback correction."""

    _global_lock = threading.RLock()

    def __init__(self, store: Store, clock=time.time) -> None:
        self._store = store
        self._clock = clock
        self._well = Well(store)
        self._flow_repository = FlowRepository(store)
        self._auditor = Auditor(store)

    def config(self) -> ChlorConfig:
        raw, present = self._store.get(CONFIG_KEY)
        if not present:
            return ChlorConfig()
        try:
            payload = json.loads(raw)
        except ValueError:
            return ChlorConfig()
        if not isinstance(payload, dict):
            return ChlorConfig()
        allowed = ChlorConfig().__dict__
        values = {key: payload[key] for key in allowed if key in payload}
        return ChlorConfig(**values)

    def configure(self, **changes: float | int) -> ChlorConfig:
        """Validate and persist the policy/model configuration."""

        current = self.config()
        config = replace(current, **changes)
        self._validate_config(config)
        self._store.put(CONFIG_KEY, json.dumps(config.as_dict(), ensure_ascii=False))
        return config

    def state(self) -> ChlorinationState:
        config = self.config()
        state = self._load_state()
        active = None if state.get("active") is None else self._round_from_dict(state["active"])
        history = [self._round_from_dict(item) for item in state.get("history", [])]
        processed = dict(state.get("processed", {}))
        return ChlorinationState(
            config=config,
            active_round=active,
            last_round=history[-1] if history else None,
            rounds=history,
            learned_bias_mg_l=float(state.get("bias", 0.0)),
            last_ammonia_n_mg_l=state.get("last_ammonia"),
            processed_requests=processed,
            round_count=int(state.get("round_sequence", 0)),
        )

    def describe(self) -> str:
        state = self.state()
        active = "none"
        if state.active_round is not None:
            active = (
                f"{state.active_round.round_id}:{state.active_round.status}:"
                f"actions={len(state.active_round.actions)}"
            )
        return (
            f"chlorination rounds={state.round_count} active={active} "
            f"bias={state.learned_bias_mg_l:.4f}"
        )

    def current_flow(self, flow_m3h: float | None = None) -> float:
        """Return the supplied or measured flow; zero preserves design fallback."""

        if flow_m3h is not None:
            return flow_m3h
        value, present = self._flow_repository.load_flow()
        return value if present else self.config().design_flow_m3h

    def start_round(
        self,
        ammonia_n_mg_l: float,
        request_id: str,
        target_residual_mg_l: float | None = None,
        flow_m3h: float | None = None,
    ) -> dict[str, object]:
        """Issue the first feedforward dose for one clear-well parcel."""

        self._require_request_id(request_id)
        validate_ammonia(ammonia_n_mg_l)
        target = self._well.residual_target() if target_residual_mg_l is None else target_residual_mg_l
        validate_positive(target, "target residual", 5.0)
        flow = self.current_flow(flow_m3h)
        if flow < 0:
            raise ValueError("flow must be non-negative")

        with self._global_lock:
            cached = self._processed(request_id)
            if cached is not None:
                return cached
            state = self._load_state()
            if state.get("active") is not None:
                raise ChlorinationBusyError(
                    "active chlorine round must receive its actual residual before the next round"
                )

            config = self.config()
            now = self._now()
            previous_ammonia = state.get("last_ammonia")
            quality = select_water_quality(
                ammonia_n_mg_l,
                None if previous_ammonia is None else float(previous_ammonia),
                config,
            )
            if quality == "stable":
                strategy = "feedforward_full_then_feedback"
                fraction = config.stable_feedforward_fraction
                feedback_rounds = config.full_step_feedback_rounds
            else:
                strategy = "small_steps_remeasure_after_each_hydraulic_delay"
                fraction = config.variable_feedforward_fraction
                feedback_rounds = config.small_step_feedback_rounds

            hrt, basis, effective_flow = contact_time_minutes(flow, config)
            bias = float(state.get("bias", 0.0))
            demand = model_demand_mg_l(target, ammonia_n_mg_l, hrt, config, bias)
            dose_concentration = demand * fraction
            round_id = self._next_round_id(state)
            action = DoseAction(
                sequence=1,
                phase="feedforward",
                at=now,
                due_at=now + hrt * 60.0,
                target_residual_mg_l=target,
                ammonia_n_mg_l=ammonia_n_mg_l,
                flow_m3h=effective_flow,
                contact_time_minutes=hrt,
                contact_basis=basis,
                model_demand_mg_l=demand,
                dose_concentration_mg_l=dose_concentration,
                dose_rate_kg_h=dose_rate_kg_h(dose_concentration, effective_flow),
                cumulative_concentration_mg_l=dose_concentration,
            )
            round_item = DosingRound(
                round_id=round_id,
                request_id=request_id,
                started_at=now,
                target_residual_mg_l=target,
                ammonia_n_mg_l=ammonia_n_mg_l,
                previous_ammonia_n_mg_l=None if previous_ammonia is None else float(previous_ammonia),
                water_quality=quality,
                strategy=strategy,
                feedforward_fraction=fraction,
                max_feedback_rounds=feedback_rounds,
                bias_before_mg_l=bias,
                status="waiting_residual",
                actions=[action],
                due_at=action.due_at,
            )
            audit_payload, _ = self._auditor.entry_payload(
                FEEDFORWARD_KIND,
                f"{action.dose_rate_kg_h:.4f}",
                {
                    "round_id": round_id,
                    "sequence": 1,
                    "phase": "feedforward",
                    "mg_l": self._number(action.dose_concentration_mg_l),
                    "kg_h": self._number(action.dose_rate_kg_h),
                    "target_mg_l": self._number(target),
                    "ammonia_n_mg_l": self._number(ammonia_n_mg_l),
                    "contact_time_minutes": self._number(hrt),
                    "contact_basis": basis,
                    "request_id": request_id,
                },
            )
            state["active"] = round_item.as_dict()
            state["last_ammonia"] = ammonia_n_mg_l
            result = {
                "idempotent": False,
                "round": round_item.as_dict(),
                "action": action.as_dict(),
            }
            self._remember(state, request_id, "start_round", result)
            self._save_with_audit(state, [audit_payload])
            return result

    def observe_residual(
        self,
        residual_mg_l: float,
        reading_id: str,
        request_id: str,
        recovered: bool = False,
    ) -> dict[str, object]:
        """Apply a residual confirmed after the round's hydraulic delay."""

        self._require_request_id(request_id)
        self._require_reading_id(reading_id)
        validate_residual(residual_mg_l)
        with self._global_lock:
            cached = self._find_cached_reading(request_id, reading_id)
            if cached is not None:
                return cached
            state = self._load_state()
            active_payload = state.get("active")
            if active_payload is None:
                raise ChlorinationBusyError("no active chlorine round is waiting for a residual")
            active = self._round_from_dict(active_payload)
            if active.status == "residual_missing" and not recovered:
                raise ChlorinationBusyError(
                    "residual reading is marked lost; use the recovery transition"
                )
            if active.status not in ("waiting_residual", "residual_missing"):
                raise ChlorinationBusyError(f"round is {active.status}")
            now = self._now()
            if now < active.due_at:
                raise ChlorinationBusyError(
                    f"residual is not due until {active.due_at:.3f}"
                )

            error = residual_mg_l - active.target_residual_mg_l
            audit_payloads: list[str] = []
            observation = ResidualObservation(
                reading_id=reading_id,
                request_id=request_id,
                at=now,
                residual_mg_l=residual_mg_l,
                expected_after_action=len(active.actions),
                error_mg_l=error,
                recovered=recovered,
            )
            active = self._append_observation(active, observation)
            if recovered:
                active = replace(
                    active,
                    events=active.events
                    + [
                        StatusEvent(
                            kind="recovered",
                            at=now,
                            request_id=request_id,
                            reading_id=reading_id,
                        )
                    ],
                )
                recovery_payload, _ = self._auditor.entry_payload(RECOVERY_KIND, reading_id)
                audit_payloads.append(recovery_payload)

            action: DoseAction | None = None
            closed = False
            feedback_count = len(active.observations) - 1
            if abs(error) <= self.config().residual_tolerance_mg_l:
                active = self._close_round(
                    active, state, "residual_in_tolerance", residual_mg_l, error, now
                )
                closed = True
            elif error > 0:
                active = self._close_round(
                    active, state, "residual_high_no_negative_dose", residual_mg_l, error, now
                )
                closed = True
            elif feedback_count >= active.max_feedback_rounds:
                active = self._close_round(
                    active, state, "feedback_round_limit", residual_mg_l, error, now
                )
                closed = True
            else:
                action = self._feedback_action(active, error, now)
                active = replace(
                    active,
                    actions=active.actions + [action],
                    status="waiting_residual",
                    due_at=action.due_at,
                )
                feedback_payload, _ = self._auditor.entry_payload(
                    FEEDBACK_KIND,
                    f"{action.dose_rate_kg_h:.4f}",
                    {
                        "round_id": active.round_id,
                        "sequence": action.sequence,
                        "phase": "feedback",
                        "mg_l": self._number(action.dose_concentration_mg_l),
                        "kg_h": self._number(action.dose_rate_kg_h),
                        "contact_time_minutes": self._number(action.contact_time_minutes),
                        "contact_basis": action.contact_basis,
                        "flow_m3h": self._number(action.flow_m3h),
                        "residual_mg_l": self._number(residual_mg_l),
                        "error_mg_l": self._number(error),
                        "reading_id": reading_id,
                        "request_id": request_id,
                        "recovered": recovered,
                    },
                )
                audit_payloads.append(feedback_payload)

            result = {
                "idempotent": False,
                "round": active.as_dict(),
                "observation": observation.as_dict(),
                "action": None if action is None else action.as_dict(),
                "closed": closed,
            }
            self._remember(
                state,
                request_id,
                "recover_residual" if recovered else "observe_residual",
                result,
                reading_id=reading_id,
            )
            state["active"] = None if closed else active.as_dict()
            if closed:
                self._archive(state, active)
            self._save_with_audit(state, audit_payloads)
            return result

    def report_missing_residual(self, request_id: str) -> dict[str, object]:
        """Mark the active residual observation as a recoverable partial failure."""

        self._require_request_id(request_id)
        with self._global_lock:
            cached = self._processed(request_id)
            if cached is not None:
                return cached
            state = self._load_state()
            active_payload = state.get("active")
            if active_payload is None:
                raise ChlorinationBusyError("no active chlorine round can report a missing reading")
            active = self._round_from_dict(active_payload)
            if active.status == "residual_missing":
                result = {
                    "idempotent": True,
                    "round": active.as_dict(),
                    "missing": True,
                }
                self._remember(state, request_id, "report_missing_residual", result)
                self._save_with_audit(state)
                return result
            if active.status != "waiting_residual":
                raise ChlorinationBusyError(f"round is {active.status}")
            now = self._now()
            active = replace(
                active,
                events=active.events
                + [StatusEvent(kind="missing", at=now, request_id=request_id)],
                status="residual_missing",
                missing_since=now,
            )
            missing_payload, _ = self._auditor.entry_payload(
                MISSING_KIND, active.round_id, {"round_id": active.round_id, "request_id": request_id}
            )
            state["active"] = active.as_dict()
            result = {"idempotent": False, "round": active.as_dict(), "missing": True}
            self._remember(state, request_id, "report_missing_residual", result)
            self._save_with_audit(state, [missing_payload])
            return result

    def recover_residual(
        self, residual_mg_l: float, reading_id: str, request_id: str
    ) -> dict[str, object]:
        """Resume a missing-reading round using the recovered analyzer value."""

        return self.observe_residual(
            residual_mg_l=residual_mg_l,
            reading_id=reading_id,
            request_id=request_id,
            recovered=True,
        )

    def _feedback_action(self, active: DosingRound, error: float, now: float) -> DoseAction:
        config = self.config()
        previous = active.actions[-1]
        fraction = config.feedback_fraction if active.water_quality == "stable" else config.trim_fraction
        correction = abs(error) * fraction
        cumulative = previous.cumulative_concentration_mg_l + correction
        return DoseAction(
            sequence=len(active.actions) + 1,
            phase="feedback",
            at=now,
            due_at=now + previous.contact_time_minutes * 60.0,
            target_residual_mg_l=active.target_residual_mg_l,
            ammonia_n_mg_l=active.ammonia_n_mg_l,
            flow_m3h=previous.flow_m3h,
            contact_time_minutes=previous.contact_time_minutes,
            contact_basis=previous.contact_basis,
            model_demand_mg_l=previous.model_demand_mg_l,
            dose_concentration_mg_l=correction,
            dose_rate_kg_h=dose_rate_kg_h(correction, previous.flow_m3h),
            cumulative_concentration_mg_l=cumulative,
        )

    def _close_round(
        self,
        active: DosingRound,
        state: dict[str, Any],
        reason: str,
        final_residual: float,
        final_error: float,
        now: float,
    ) -> DosingRound:
        first_error = active.observations[0].error_mg_l if active.observations else None
        bias = active.bias_before_mg_l
        if first_error is not None:
            config = self.config()
            bias = max(
                -config.max_bias_mg_l,
                min(config.max_bias_mg_l, bias - config.bias_learning_gain * first_error),
            )
        state["bias"] = self._number(bias)
        return replace(
            active,
            status="closed",
            first_error_mg_l=first_error,
            final_error_mg_l=final_error,
            final_residual_mg_l=final_residual,
            closed_at=now,
            bias_after_mg_l=bias,
            closed_reason=reason,
            missing_since=None,
        )

    def _append_observation(
        self, active: DosingRound, observation: ResidualObservation
    ) -> DosingRound:
        for existing in active.observations:
            if existing.reading_id == observation.reading_id:
                return replace(active)
        return replace(active, observations=active.observations + [observation])

    def _validate_config(self, config: ChlorConfig) -> None:
        validate_positive(config.volume_m3, "clearwell volume", 1_000_000_000.0)
        validate_positive(config.design_hrt_minutes, "design HRT", 10_000.0)
        validate_positive(config.design_flow_m3h, "design flow", 1_000_000.0)
        if config.decay_mg_per_l_h < 0 or config.decay_mg_per_l_h > 10:
            raise ValueError("decay rate must be between zero and ten mg/L/h")
        if config.breakpoint_ratio < 0 or config.breakpoint_ratio > 100:
            raise ValueError("breakpoint ratio must be between zero and one hundred")
        validate_ammonia(config.ammonia_threshold_mg_l)
        validate_ammonia(config.ammonia_change_mg_l)
        validate_fraction(config.stable_feedforward_fraction, "stable feedforward fraction")
        validate_fraction(config.variable_feedforward_fraction, "variable feedforward fraction")
        validate_fraction(config.feedback_fraction, "feedback fraction")
        validate_fraction(config.trim_fraction, "trim fraction")
        if not 1 <= config.full_step_feedback_rounds <= 10:
            raise ValueError("full-step feedback rounds must be between one and ten")
        if not 1 <= config.small_step_feedback_rounds <= 10:
            raise ValueError("small-step feedback rounds must be between one and ten")
        validate_positive(config.residual_tolerance_mg_l, "residual tolerance", 1.0)
        if config.bias_learning_gain < 0 or config.bias_learning_gain > 1:
            raise ValueError("bias learning gain must be between zero and one")
        validate_positive(config.max_bias_mg_l, "maximum bias", 20.0)

    def _load_state(self) -> dict[str, Any]:
        raw, present = self._store.get(STATE_KEY)
        if not present:
            return {
                "round_sequence": 0,
                "bias": 0.0,
                "last_ammonia": None,
                "active": None,
                "history": [],
                "processed": {},
            }
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return {
            "round_sequence": int(payload.get("round_sequence", 0)),
            "bias": float(payload.get("bias", 0.0)),
            "last_ammonia": payload.get("last_ammonia"),
            "active": payload.get("active"),
            "history": payload.get("history", []) if isinstance(payload.get("history"), list) else [],
            "processed": payload.get("processed", {})
            if isinstance(payload.get("processed"), dict)
            else {},
        }

    def _save_with_audit(
        self, state: dict[str, Any], audit_payloads: list[str] | None = None
    ) -> None:
        """Commit controller state and audit entries in one document update."""

        values: dict[str, str] = {STATE_KEY: json.dumps(state, ensure_ascii=False)}
        if audit_payloads:
            entries = load_commands(self._store, AUDIT_KEY)
            entries.extend(audit_payloads)
            values[AUDIT_KEY] = json.dumps(entries, ensure_ascii=False)
        self._store.put_many(values)

    def _next_round_id(self, state: dict[str, Any]) -> str:
        sequence = int(state.get("round_sequence", 0)) + 1
        state["round_sequence"] = sequence
        return f"CL-{sequence:06d}"

    def _archive(self, state: dict[str, Any], active: DosingRound) -> None:
        history = list(state.get("history", []))
        history.append(active.as_dict())
        if len(history) > MAX_HISTORY:
            history = history[-MAX_HISTORY:]
        state["history"] = history

    def _remember(
        self,
        state: dict[str, Any],
        request_id: str,
        kind: str,
        result: dict[str, object],
        reading_id: str = "",
    ) -> None:
        processed = dict(state.get("processed", {}))
        processed[request_id] = {
            "kind": kind,
            "reading_id": reading_id,
            "result": result,
        }
        if len(processed) > MAX_PROCESSED_REQUESTS:
            for key in list(processed)[:-MAX_PROCESSED_REQUESTS]:
                del processed[key]
        state["processed"] = processed

    def _processed(self, request_id: str) -> dict[str, object] | None:
        entry = self._load_state().get("processed", {}).get(request_id)
        if not isinstance(entry, dict):
            return None
        result = entry.get("result")
        if not isinstance(result, dict):
            return None
        cached = dict(result)
        cached["idempotent"] = True
        return cached

    def _find_cached_reading(
        self, request_id: str, reading_id: str
    ) -> dict[str, object] | None:
        cached = self._processed(request_id)
        if cached is not None:
            return cached
        state = self._load_state()
        active = state.get("active")
        candidates: list[object] = [active] if active else []
        candidates.extend(state.get("history", []))
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            for observation in candidate.get("observations", []):
                if isinstance(observation, dict) and observation.get("reading_id") == reading_id:
                    round_item = self._round_from_dict(candidate)
                    matching = next(
                        item for item in round_item.observations if item.reading_id == reading_id
                    )
                    return {
                        "idempotent": True,
                        "round": round_item.as_dict(),
                        "observation": replace(matching, idempotent=True).as_dict(),
                        "action": None,
                        "closed": round_item.status == "closed",
                    }
        return None

    def _round_from_dict(self, payload: dict[str, Any]) -> DosingRound:
        return DosingRound(
            round_id=str(payload.get("round_id", "")),
            request_id=str(payload.get("request_id", "")),
            started_at=float(payload.get("started_at", 0.0)),
            target_residual_mg_l=float(payload.get("target_residual_mg_l", 0.0)),
            ammonia_n_mg_l=float(payload.get("ammonia_n_mg_l", 0.0)),
            previous_ammonia_n_mg_l=payload.get("previous_ammonia_n_mg_l"),
            water_quality=self._enum(payload.get("water_quality"), ("stable", "variable"), "stable"),
            strategy=str(payload.get("strategy", "")),
            feedforward_fraction=float(payload.get("feedforward_fraction", 0.0)),
            max_feedback_rounds=int(payload.get("max_feedback_rounds", 0)),
            bias_before_mg_l=float(payload.get("bias_before_mg_l", 0.0)),
            status=self._enum(
                payload.get("status"),
                ("waiting_residual", "residual_missing", "closed"),
                "waiting_residual",
            ),
            actions=[self._action_from_dict(item) for item in payload.get("actions", [])],
            observations=[
                self._observation_from_dict(item) for item in payload.get("observations", [])
            ],
            events=[self._event_from_dict(item) for item in payload.get("events", [])],
            due_at=float(payload.get("due_at", 0.0)),
            first_error_mg_l=payload.get("first_error_mg_l"),
            final_error_mg_l=payload.get("final_error_mg_l"),
            final_residual_mg_l=payload.get("final_residual_mg_l"),
            closed_at=payload.get("closed_at"),
            bias_after_mg_l=payload.get("bias_after_mg_l"),
            closed_reason=str(payload.get("closed_reason", "")),
            missing_since=payload.get("missing_since"),
        )

    def _action_from_dict(self, payload: dict[str, Any]) -> DoseAction:
        return DoseAction(
            sequence=int(payload.get("sequence", 0)),
            phase=self._enum(payload.get("phase"), ("feedforward", "feedback"), "feedforward"),
            at=float(payload.get("at", 0.0)),
            due_at=float(payload.get("due_at", 0.0)),
            target_residual_mg_l=float(payload.get("target_residual_mg_l", 0.0)),
            ammonia_n_mg_l=float(payload.get("ammonia_n_mg_l", 0.0)),
            flow_m3h=float(payload.get("flow_m3h", 0.0)),
            contact_time_minutes=float(payload.get("contact_time_minutes", 0.0)),
            contact_basis=self._enum(
                payload.get("contact_basis"), ("measured_flow", "design_fallback"), "measured_flow"
            ),
            model_demand_mg_l=float(payload.get("model_demand_mg_l", 0.0)),
            dose_concentration_mg_l=float(payload.get("dose_concentration_mg_l", 0.0)),
            dose_rate_kg_h=float(payload.get("dose_rate_kg_h", 0.0)),
            cumulative_concentration_mg_l=float(payload.get("cumulative_concentration_mg_l", 0.0)),
            idempotent=bool(payload.get("idempotent", False)),
        )

    def _observation_from_dict(self, payload: dict[str, Any]) -> ResidualObservation:
        return ResidualObservation(
            reading_id=str(payload.get("reading_id", "")),
            request_id=str(payload.get("request_id", "")),
            at=float(payload.get("at", 0.0)),
            residual_mg_l=float(payload.get("residual_mg_l", 0.0)),
            expected_after_action=int(payload.get("expected_after_action", 0)),
            error_mg_l=float(payload.get("error_mg_l", 0.0)),
            recovered=bool(payload.get("recovered", False)),
            idempotent=bool(payload.get("idempotent", False)),
        )

    def _event_from_dict(self, payload: dict[str, Any]) -> StatusEvent:
        return StatusEvent(
            kind=str(payload.get("kind", "")),
            at=float(payload.get("at", 0.0)),
            request_id=str(payload.get("request_id", "")),
            reading_id=str(payload.get("reading_id", "")),
        )

    def _enum(self, value: object, allowed: tuple[str, ...], default: str) -> str:
        text = str(value)
        return text if text in allowed else default

    def _now(self) -> float:
        return float(self._clock())

    def _number(self, value: float) -> float:
        return round(float(value), 6)

    def _require_request_id(self, request_id: str) -> None:
        if not request_id:
            raise ValueError("request_id is required for idempotent control transitions")

    def _require_reading_id(self, reading_id: str) -> None:
        if not reading_id:
            raise ValueError("reading_id is required to deduplicate residual readings")
