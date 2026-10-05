"""Chlorine dosing controller with feedforward and gated feedback."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

from waterplant.audit import Auditor
from waterplant.clearwell import Well
from waterplant.flow import Calibration
from waterplant.intake import FlowRepository
from waterplant.store.epoch import load_float, save_float
from waterplant.store.store import Store

from .errors import ChlorRoundConflict
from .hydraulics import Hydraulics
from .report import (
    DEFAULT_AMMONIA_CHANGE_THRESHOLD,
    DEFAULT_AMMONIA_INCREMENTAL_THRESHOLD,
    DEFAULT_AMMONIA_CHLORINE_RATIO,
    DEFAULT_FEEDBACK_CAP,
    DEFAULT_FEEDBACK_GAIN,
    DEFAULT_RESIDUAL_DEADBAND,
    DEFAULT_RESIDUAL_DECAY_RATE,
    SMALL_STEP_RETEST_MINUTES,
    ChlorState,
    DosePlan,
    DoseStrategy,
    FeedbackResult,
    RoundStatus,
    WaterPolicy,
    base_dose as calculate_base_dose,
    feedback_correction,
    feedforward_dose,
    update_bias,
    validate_ammonia,
    validate_elapsed_minutes,
    validate_gain,
    validate_residual,
)
from .round import RoundRepository, RoundState

CHLORINE = "chlorine"
AMMONIA_KEY = "chlor:ammonia-n"
BIAS_KEY = "chlor:learned-bias"
GAIN_KEY = "chlor:feedback-gain"
CAP_KEY = "chlor:feedback-cap"
DEADBAND_KEY = "chlor:residual-deadband"
DECAY_KEY = "chlor:residual-decay-rate"
RATIO_KEY = "chlor:ammonia-ratio"
THRESHOLD_KEY = "chlor:ammonia-incremental-threshold"
CHANGE_KEY = "chlor:ammonia-change-threshold"


class Doser:
    """Controls chlorine using residual target, ammonia and contact time."""

    def __init__(
        self,
        store: Store,
        clock: Callable[[], float] | None = None,
        auditor: Auditor | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or time.time
        self._well = Well(store)
        self._hydraulics = Hydraulics(store)
        self._flow = FlowRepository(store)
        self._calibration = Calibration(store)
        self._auditor = auditor or Auditor(store, self._clock)
        self._rounds = RoundRepository(store, self._clock)

    @property
    def rounds(self) -> RoundRepository:
        return self._rounds

    @property
    def hydraulics(self) -> Hydraulics:
        return self._hydraulics

    def current_target(self) -> float:
        """Residual target read fresh from the clear well on every call."""

        return self._well.residual_target()

    def set_target(self, target: float) -> float:
        validate_residual(target, "target")
        self._well.set_residual_target(target)
        return target

    def target_for_demand(self, demand: float) -> float:
        return 0.3 + demand * 0.7

    def set_target_from_demand(self, demand: float) -> float:
        target = self.target_for_demand(demand)
        self.set_target(target)
        return target

    def current_ammonia(self) -> float:
        value, present = load_float(self._store, AMMONIA_KEY)
        return value if present else 0.0

    def set_ammonia(self, value: float) -> None:
        validate_ammonia(value)
        save_float(self._store, AMMONIA_KEY, value)

    def learned_bias(self) -> float:
        value, _ = load_float(self._store, BIAS_KEY)
        return value

    def set_learned_bias(self, value: float) -> None:
        save_float(self._store, BIAS_KEY, max(-5.0, min(5.0, value)))

    def feedback_gain(self) -> float:
        value, present = load_float(self._store, GAIN_KEY)
        return value if present else DEFAULT_FEEDBACK_GAIN

    def set_feedback_gain(self, value: float) -> None:
        validate_gain(value)
        save_float(self._store, GAIN_KEY, value)

    def feedback_cap(self) -> float:
        value, present = load_float(self._store, CAP_KEY)
        return value if present else DEFAULT_FEEDBACK_CAP

    def set_feedback_cap(self, value: float) -> None:
        if value < 0 or value > 10:
            raise ValueError("feedback cap must be between 0 and 10")
        save_float(self._store, CAP_KEY, value)

    def residual_deadband(self) -> float:
        value, present = load_float(self._store, DEADBAND_KEY)
        return value if present else DEFAULT_RESIDUAL_DEADBAND

    def set_residual_deadband(self, value: float) -> None:
        if value < 0 or value > 1:
            raise ValueError("residual deadband must be between 0 and 1")
        save_float(self._store, DEADBAND_KEY, value)

    def residual_decay_rate(self) -> float:
        value, present = load_float(self._store, DECAY_KEY)
        return value if present else DEFAULT_RESIDUAL_DECAY_RATE

    def set_residual_decay_rate(self, value: float) -> None:
        if value < 0 or value > 5:
            raise ValueError("residual decay rate must be between 0 and 5")
        save_float(self._store, DECAY_KEY, value)

    def ammonia_ratio(self) -> float:
        value, present = load_float(self._store, RATIO_KEY)
        return value if present else DEFAULT_AMMONIA_CHLORINE_RATIO

    def set_ammonia_ratio(self, value: float) -> None:
        if value <= 0 or value > 30:
            raise ValueError("ammonia chlorine ratio must be positive and at most 30")
        save_float(self._store, RATIO_KEY, value)

    def policy(self) -> WaterPolicy:
        high, present_high = load_float(self._store, THRESHOLD_KEY)
        change, present_change = load_float(self._store, CHANGE_KEY)
        return WaterPolicy(
            high if present_high else DEFAULT_AMMONIA_INCREMENTAL_THRESHOLD,
            change if present_change else DEFAULT_AMMONIA_CHANGE_THRESHOLD,
        )

    def set_water_policy(self, high_threshold: float, change_threshold: float) -> None:
        validate_ammonia(high_threshold)
        if change_threshold < 0 or change_threshold > 50:
            raise ValueError("ammonia change threshold must be non-negative")
        save_float(self._store, THRESHOLD_KEY, high_threshold)
        save_float(self._store, CHANGE_KEY, change_threshold)

    def measured_flow(self) -> float | None:
        raw, present = self._flow.load_flow()
        if not present or raw <= 0:
            return None
        return raw * self._calibration.current()

    def plan_dose(self, ammonia_n: float | None = None) -> DosePlan:
        """Calculate one feedforward dose from target, ammonia and HRT."""

        target = self.current_target()
        previous_ammonia = self.current_ammonia()
        if ammonia_n is None:
            ammonia_n = previous_ammonia
        validate_residual(target, "target")
        validate_ammonia(ammonia_n)

        prior_for_policy = previous_ammonia if self._rounds.list_rounds() else None
        hydraulics = self._hydraulics.calculate(self.measured_flow())
        bias = self.learned_bias()
        full_dose, dose, strategy, buildup = feedforward_dose(
            target=target,
            ammonia_n=ammonia_n,
            previous_ammonia_n=prior_for_policy,
            contact_minutes=hydraulics.contact_minutes,
            learned_bias=bias,
            policy=self.policy(),
            ammonia_ratio=self.ammonia_ratio(),
            decay_rate_per_hour=self.residual_decay_rate(),
        )
        base = calculate_base_dose(
            target,
            ammonia_n,
            contact_minutes=hydraulics.contact_minutes,
            ammonia_ratio=self.ammonia_ratio(),
            decay_rate_per_hour=self.residual_decay_rate(),
            learned_bias=bias,
        )
        feedback_minutes = (
            hydraulics.contact_minutes
            if strategy is DoseStrategy.FULL
            else min(SMALL_STEP_RETEST_MINUTES, hydraulics.contact_minutes)
        )
        first_round = not self._rounds.list_rounds()
        return DosePlan(
            target=target,
            ammonia_n=ammonia_n,
            previous_ammonia_n=previous_ammonia,
            ammonia_change=max(0.0, ammonia_n - previous_ammonia),
            base_dose=max(0.0, base),
            full_dose=full_dose,
            strategy=strategy,
            buildup=buildup,
            dose_mg_l=dose,
            flow_m3_h=hydraulics.measured_flow,
            dose_kg_h=hydraulics.measured_flow * dose / 1000.0,
            contact_minutes=hydraulics.contact_minutes,
            feedback_minutes=feedback_minutes,
            contact_source=hydraulics.contact_source,
            learned_bias=bias,
            first_round=first_round,
        )

    def start_round(
        self,
        ammonia_n: float | None = None,
        *,
        request_id: str = "",
        round_id: str = "",
    ) -> tuple[RoundState, bool]:
        """Apply a feedforward dose and open its feedback gate."""

        with self._store.mutate():
            existing = self._rounds.current()
            if existing is not None:
                if request_id and existing.request_id == request_id:
                    return existing, False
                if existing.status == RoundStatus.READING_LOST:
                    raise ChlorRoundConflict(
                        "recover or cancel the previous chlorine round first"
                    )
                raise ChlorRoundConflict("previous residual must be confirmed first")

            plan = self.plan_dose(ammonia_n)
            round_state = self._rounds.start(plan, request_id, round_id)
            self.set_ammonia(plan.ammonia_n)
            self._rounds.save(round_state)
            self._auditor.record_once(
                f"{round_state.round_id}:feedforward",
                kind=CHLORINE,
                detail=f"feedforward={plan.dose_mg_l:.4f}mg/L",
                amount=plan.dose_mg_l,
                phase="feedforward",
                round_id=round_state.round_id,
                payload={**plan.as_dict(), "mass_kg_h": plan.dose_kg_h},
            )
            return round_state, True

    def apply_residual(self, ammonia_n: float | None = None) -> float:
        """Backward-compatible command for starting a feedforward round."""

        return self.start_round(ammonia_n)[0].plan["dose_mg_l"]

    def _require_active_round(self, round_id: str) -> RoundState:
        round_state = self._rounds.current()
        if round_state is None:
            raise ChlorRoundConflict("no active chlorine round")
        if round_id and round_state.round_id != round_id:
            raise ChlorRoundConflict("round id does not match the active round")
        return round_state

    def mark_reading_lost(
        self,
        *,
        round_id: str = "",
        loss_id: str = "",
        reason: str = "residual reading unavailable",
    ) -> tuple[RoundState, bool]:
        """Persist partial failure without dosing or closing the round."""

        with self._store.mutate():
            round_state = self._require_active_round(round_id)
            if round_state.status == RoundStatus.READING_LOST:
                return round_state, False
            if round_state.feedback is not None:
                raise ChlorRoundConflict("round already has feedback")
            updated = round_state.replace(
                status=RoundStatus.READING_LOST,
                lost_at=self._clock(),
                loss_id=loss_id or f"loss-{uuid.uuid4().hex}",
            )
            self._rounds.save(updated)
            self._auditor.record_once(
                f"{updated.round_id}:reading-lost",
                kind="chlorine_reading_lost",
                detail=reason,
                amount=0.0,
                phase="feedback_wait",
                round_id=updated.round_id,
                payload={"reason": reason, "loss_id": updated.loss_id},
            )
            return updated, True

    def report_feedback(
        self,
        measured_residual: float,
        *,
        elapsed_minutes: float | None = None,
        round_id: str = "",
        reading_id: str = "",
        feedback_id: str = "",
        request_id: str = "",
        recovery: bool = False,
    ) -> tuple[RoundState, FeedbackResult, bool]:
        """Confirm the round after contact time and apply one correction."""

        validate_residual(measured_residual, "measured_residual")
        with self._store.mutate():
            if reading_id:
                completed = self._rounds.find(reading_id=reading_id)
                if completed is not None and completed.feedback is not None:
                    feedback = completed.feedback_result()
                    assert feedback is not None
                    return completed, feedback, False
            if feedback_id:
                completed = self._rounds.find(recovery_id=feedback_id)
                if completed is not None and completed.feedback is not None:
                    feedback = completed.feedback_result()
                    assert feedback is not None
                    return completed, feedback, False
            if request_id and recovery:
                completed = self._rounds.find(recovery_id=request_id)
                if completed is not None and completed.feedback is not None:
                    feedback = completed.feedback_result()
                    assert feedback is not None
                    return completed, feedback, False
            round_state = self._require_active_round(round_id)

            if round_state.feedback is not None:
                feedback = round_state.feedback_result()
                assert feedback is not None
                return round_state, feedback, False
            if recovery and round_state.status != RoundStatus.READING_LOST:
                raise ChlorRoundConflict("round is not waiting for reading recovery")
            if not recovery and round_state.status == RoundStatus.READING_LOST:
                raise ChlorRoundConflict("mark the residual reading recovered first")

            elapsed = self._elapsed(round_state, elapsed_minutes)
            if elapsed < round_state.plan["feedback_minutes"]:
                raise ChlorRoundConflict(
                    f"feedback time not reached: {elapsed:.3f} < "
                    f"{round_state.plan['feedback_minutes']:.3f} minutes"
                )

            plan = round_state.dose_plan()
            correction = feedback_correction(
                plan.target,
                measured_residual,
                self.feedback_gain(),
                self.feedback_cap(),
                self.residual_deadband(),
            )
            applied = max(0.0, correction)
            flow = plan.flow_m3_h
            bias_before = plan.learned_bias
            bias_after = update_bias(bias_before, correction)
            self.set_learned_bias(bias_after)
            result = FeedbackResult(
                round_id=round_state.round_id,
                measured_residual=measured_residual,
                error=plan.target - measured_residual,
                correction_mg_l=correction,
                correction_kg_h=flow * correction / 1000.0,
                applied_correction_mg_l=applied,
                applied_correction_kg_h=flow * applied / 1000.0,
                total_applied_mg_l=plan.dose_mg_l + applied,
                total_applied_kg_h=flow * (plan.dose_mg_l + applied) / 1000.0,
                elapsed_minutes=elapsed,
                deadband=self.residual_deadband(),
                bias_before=bias_before,
                bias_after=bias_after,
                reading_id=reading_id or f"reading-{uuid.uuid4().hex}",
                recovery=round_state.status == RoundStatus.READING_LOST,
            )
            status = (
                RoundStatus.NO_CORRECTION if correction == 0.0 else RoundStatus.CORRECTED
            )
            updated = round_state.replace(
                status=status,
                feedback=result.as_dict(),
                reading_id=result.reading_id,
                recovery_id=request_id if recovery else (feedback_id or round_state.recovery_id),
            )
            self._rounds.save(updated)
            self._auditor.record_once(
                f"{updated.round_id}:feedback:{feedback_id or request_id or result.reading_id}",
                kind=CHLORINE,
                detail=(
                    f"residual={measured_residual:.4f} "
                    f"correction={result.applied_correction_mg_l:.4f}mg/L"
                ),
                amount=result.applied_correction_mg_l,
                phase="feedback",
                round_id=updated.round_id,
                payload=result.as_dict(),
            )
            self._rounds.finish(updated)
            return updated, result, True

    def recover_reading(
        self,
        measured_residual: float,
        *,
        elapsed_minutes: float | None = None,
        round_id: str = "",
        reading_id: str = "",
        request_id: str = "",
    ) -> tuple[RoundState, FeedbackResult, bool]:
        """Complete a round previously parked because its reading was missing."""

        normalized_request = request_id or f"recover-{reading_id or round_id or 'round'}"
        with self._store.mutate():
            completed = self._rounds.find(recovery_id=normalized_request, reading_id=reading_id)
            if completed is not None and completed.feedback is not None:
                feedback = completed.feedback_result()
                assert feedback is not None
                return completed, feedback, False
            if not round_id and not reading_id and not request_id:
                latest = self._rounds.latest()
                if latest is not None and latest.feedback is not None:
                    feedback = latest.feedback_result()
                    assert feedback is not None
                    return latest, feedback, False
            round_state = self._require_active_round(round_id)
            if not reading_id:
                reading_id = f"reading-{round_state.loss_id or round_state.round_id}"
            if not request_id:
                normalized_request = f"recover-{round_state.loss_id or round_state.round_id}"
            if round_state.recovery_id == normalized_request and round_state.feedback is not None:
                feedback = round_state.feedback_result()
                assert feedback is not None
                return round_state, feedback, False
            return self.report_feedback(
                measured_residual,
                elapsed_minutes=elapsed_minutes,
                round_id=round_state.round_id,
                reading_id=reading_id,
                request_id=normalized_request,
                recovery=True,
            )

    def _elapsed(self, round_state: RoundState, elapsed_minutes: float | None) -> float:
        if elapsed_minutes is not None:
            validate_elapsed_minutes(elapsed_minutes)
            return elapsed_minutes
        return max(0.0, (self._clock() - round_state.started_at) / 60.0)

    def state(self) -> ChlorState:
        round_state = self._rounds.current()
        if round_state is None:
            target = self.current_target()
            hydraulics = self._hydraulics.calculate(self.measured_flow())
            plan = self.plan_dose()
            return ChlorState(
                target=target,
                ammonia_n=self.current_ammonia(),
                previous_ammonia_n=self.current_ammonia(),
                dose_mg_l=plan.dose_mg_l,
                dose_kg_h=plan.dose_kg_h,
                strategy=plan.strategy.value,
                contact_minutes=hydraulics.contact_minutes,
                feedback_minutes=plan.feedback_minutes,
                contact_source=hydraulics.contact_source,
                status=RoundStatus.COMPLETED,
                round_id="",
                rounds=len(self._rounds.list_rounds()),
                learned_bias=self.learned_bias(),
                last_feedback=self._last_feedback(),
            )
        plan = round_state.dose_plan()
        return ChlorState(
            target=round_state.target,
            ammonia_n=round_state.ammonia_n,
            previous_ammonia_n=round_state.previous_ammonia_n or 0.0,
            dose_mg_l=plan.dose_mg_l,
            dose_kg_h=plan.dose_kg_h,
            strategy=plan.strategy.value,
            contact_minutes=round_state.contact_minutes,
            feedback_minutes=plan.feedback_minutes,
            contact_source=plan.contact_source,
            status=round_state.status,
            round_id=round_state.round_id,
            rounds=len(self._rounds.list_rounds()),
            learned_bias=self.learned_bias(),
            last_feedback=round_state.feedback_result(),
        )

    def _last_feedback(self) -> FeedbackResult | None:
        rounds = self._rounds.list_rounds()
        for item in reversed(rounds):
            feedback = item.get("feedback")
            if isinstance(feedback, dict):
                return FeedbackResult(**feedback)
        return None

    def describe(self) -> str:
        state = self.state()
        return (
            f"chlorine target={state.target:.4f} ammonia_n={state.ammonia_n:.4f} "
            f"dose={state.dose_mg_l:.4f}mg/L contact={state.contact_minutes:.1f}min "
            f"strategy={state.strategy} status={state.status} round={state.round_id}"
        )
