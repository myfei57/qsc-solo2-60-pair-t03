"""Chlorine control projections, policies and validation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class DoseStrategy(str, Enum):
    """Water-quality selected feedforward shape."""

    FULL = "full_then_feedback"
    INCREMENTAL = "small_step_then_retest"


class RoundStatus(str, Enum):
    """Lifecycle of one chlorine contact/feedback round."""

    FEEDFORWARD = "feedforward_applied"
    READING_LOST = "residual_reading_lost"
    CORRECTED = "feedback_corrected"
    COMPLETED = "completed"
    NO_CORRECTION = "no_correction_required"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class HydraulicsState:
    """Clear-well contact-time configuration and latest calculation."""

    design_flow: float
    effective_volume: float
    measured_flow: float
    measured_flow_used: bool
    contact_minutes: float
    contact_source: str

    def as_dict(self) -> dict[str, object]:
        return {
            "design_flow": self.design_flow,
            "effective_volume": self.effective_volume,
            "measured_flow": self.measured_flow,
            "measured_flow_used": self.measured_flow_used,
            "contact_minutes": self.contact_minutes,
            "contact_source": self.contact_source,
        }


@dataclass(frozen=True)
class WaterPolicy:
    """Fixed policy used to choose between a full dose and small steps."""

    ammonia_incremental_threshold: float
    ammonia_change_threshold: float

    def as_dict(self) -> dict[str, float]:
        return {
            "ammonia_incremental_threshold": self.ammonia_incremental_threshold,
            "ammonia_change_threshold": self.ammonia_change_threshold,
        }


@dataclass(frozen=True)
class DosePlan:
    """The feedforward calculation and the amount actually sent."""

    target: float
    ammonia_n: float
    previous_ammonia_n: float
    ammonia_change: float
    base_dose: float
    full_dose: float
    strategy: DoseStrategy
    buildup: bool
    dose_mg_l: float
    flow_m3_h: float
    dose_kg_h: float
    contact_minutes: float
    feedback_minutes: float
    contact_source: str
    learned_bias: float
    first_round: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "ammonia_n": self.ammonia_n,
            "previous_ammonia_n": self.previous_ammonia_n,
            "ammonia_change": self.ammonia_change,
            "base_dose": self.base_dose,
            "full_dose": self.full_dose,
            "strategy": self.strategy.value,
            "buildup": self.buildup,
            "dose_mg_l": self.dose_mg_l,
            "flow_m3_h": self.flow_m3_h,
            "dose_kg_h": self.dose_kg_h,
            "contact_minutes": self.contact_minutes,
            "feedback_minutes": self.feedback_minutes,
            "contact_source": self.contact_source,
            "learned_bias": self.learned_bias,
            "first_round": self.first_round,
        }


@dataclass(frozen=True)
class FeedbackResult:
    """Measured residual and the signed correction decision."""

    round_id: str
    measured_residual: float
    error: float
    correction_mg_l: float
    correction_kg_h: float
    applied_correction_mg_l: float
    applied_correction_kg_h: float
    total_applied_mg_l: float
    total_applied_kg_h: float
    elapsed_minutes: float
    deadband: float
    bias_before: float
    bias_after: float
    reading_id: str
    recovery: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "round_id": self.round_id,
            "measured_residual": self.measured_residual,
            "error": self.error,
            "correction_mg_l": self.correction_mg_l,
            "correction_kg_h": self.correction_kg_h,
            "applied_correction_mg_l": self.applied_correction_mg_l,
            "applied_correction_kg_h": self.applied_correction_kg_h,
            "total_applied_mg_l": self.total_applied_mg_l,
            "total_applied_kg_h": self.total_applied_kg_h,
            "elapsed_minutes": self.elapsed_minutes,
            "deadband": self.deadband,
            "bias_before": self.bias_before,
            "bias_after": self.bias_after,
            "reading_id": self.reading_id,
            "recovery": self.recovery,
        }


@dataclass(frozen=True)
class ChlorState:
    """Projection consumed by the control console."""

    target: float
    ammonia_n: float
    previous_ammonia_n: float
    dose_mg_l: float
    dose_kg_h: float
    strategy: str
    contact_minutes: float
    feedback_minutes: float
    contact_source: str
    status: str
    round_id: str
    rounds: int
    learned_bias: float
    last_feedback: FeedbackResult | None

    def as_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "ammonia_n": self.ammonia_n,
            "previous_ammonia_n": self.previous_ammonia_n,
            "dose_mg_l": self.dose_mg_l,
            "dose_kg_h": self.dose_kg_h,
            "strategy": self.strategy,
            "contact_minutes": self.contact_minutes,
            "feedback_minutes": self.feedback_minutes,
            "contact_source": self.contact_source,
            "status": self.status,
            "round_id": self.round_id,
            "rounds": self.rounds,
            "learned_bias": self.learned_bias,
            "last_feedback": None if self.last_feedback is None else self.last_feedback.as_dict(),
        }


DEFAULT_AMMONIA_INCREMENTAL_THRESHOLD = 0.5
DEFAULT_AMMONIA_CHANGE_THRESHOLD = 0.2
DEFAULT_AMMONIA_CHLORINE_RATIO = 7.6
DEFAULT_FEEDBACK_GAIN = 0.8
DEFAULT_FEEDBACK_CAP = 1.0
DEFAULT_RESIDUAL_DEADBAND = 0.02
DEFAULT_RESIDUAL_DECAY_RATE = 0.03
DEFAULT_DESIGN_FLOW = 1_000.0
DEFAULT_EFFECTIVE_VOLUME = 1_000.0
MAX_CONTACT_MINUTES = 8 * 60.0
SMALL_STEP_RETEST_MINUTES = 10.0


def validate_demand(demand: float) -> None:
    """Reject residual demands outside the supported range."""

    if demand < 0:
        raise ValueError("demand must be non-negative")
    if demand > 1000:
        raise ValueError("demand exceeds the supported range")


def validate_residual(value: float, name: str = "residual") -> None:
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    if value > 20:
        raise ValueError(f"{name} exceeds the supported range")


def validate_ammonia(value: float) -> None:
    if value < 0:
        raise ValueError("ammonia_n must be non-negative")
    if value > 50:
        raise ValueError("ammonia_n exceeds the supported range")


def validate_positive(value: float, name: str, maximum: float) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    if value > maximum:
        raise ValueError(f"{name} exceeds the supported range")


def validate_elapsed_minutes(value: float) -> None:
    if value < 0:
        raise ValueError("elapsed_minutes must be non-negative")
    if value > 7 * 24 * 60:
        raise ValueError("elapsed_minutes exceeds the supported range")


def validate_gain(value: float) -> None:
    if value < 0 or value > 2:
        raise ValueError("gain must be between 0 and 2")


def select_strategy(
    ammonia_n: float,
    previous_ammonia_n: float | None,
    policy: WaterPolicy = WaterPolicy(
        DEFAULT_AMMONIA_INCREMENTAL_THRESHOLD,
        DEFAULT_AMMONIA_CHANGE_THRESHOLD,
    ),
) -> DoseStrategy:
    """High ammonia or a fast water-quality change forces small steps."""

    if ammonia_n >= policy.ammonia_incremental_threshold:
        return DoseStrategy.INCREMENTAL
    if previous_ammonia_n is None:
        return DoseStrategy.FULL
    if abs(ammonia_n - previous_ammonia_n) > policy.ammonia_change_threshold:
        return DoseStrategy.INCREMENTAL
    return DoseStrategy.FULL


def contact_time(
    effective_volume: float,
    design_flow: float,
    measured_flow: float | None = None,
) -> tuple[float, str, float, bool]:
    """Use measured flow when valid; otherwise use the locked design fallback."""

    if measured_flow is not None and measured_flow > 0:
        minutes = effective_volume / measured_flow * 60.0
        used = True
        source = "measured_flow"
        flow = measured_flow
    else:
        minutes = effective_volume / design_flow * 60.0
        used = False
        source = "design_flow"
        flow = design_flow
    return min(minutes, MAX_CONTACT_MINUTES), source, flow, used


def base_dose(
    target: float,
    ammonia_n: float,
    contact_minutes: float = 0.0,
    ammonia_ratio: float = DEFAULT_AMMONIA_CHLORINE_RATIO,
    decay_rate_per_hour: float = DEFAULT_RESIDUAL_DECAY_RATE,
    learned_bias: float = 0.0,
) -> float:
    """Chlorine needed for target, ammonia demand and contact-time decay."""

    hours = max(0.0, contact_minutes) / 60.0
    decay_allowance = target * math.expm1(decay_rate_per_hour * hours)
    return max(0.0, target + decay_allowance + ammonia_ratio * ammonia_n + learned_bias)


def feedforward_dose(
    target: float,
    ammonia_n: float,
    previous_ammonia_n: float | None,
    contact_minutes: float,
    learned_bias: float = 0.0,
    policy: WaterPolicy | None = None,
    ammonia_ratio: float = DEFAULT_AMMONIA_CHLORINE_RATIO,
    decay_rate_per_hour: float = DEFAULT_RESIDUAL_DECAY_RATE,
) -> tuple[float, float, DoseStrategy, bool]:
    """Return the calculated full dose, actual feedforward dose and strategy."""

    policy = policy or WaterPolicy(
        DEFAULT_AMMONIA_INCREMENTAL_THRESHOLD,
        DEFAULT_AMMONIA_CHANGE_THRESHOLD,
    )
    full = base_dose(
        target,
        ammonia_n,
        contact_minutes=contact_minutes,
        ammonia_ratio=ammonia_ratio,
        decay_rate_per_hour=decay_rate_per_hour,
        learned_bias=learned_bias,
    )
    strategy = select_strategy(ammonia_n, previous_ammonia_n, policy)
    if strategy is DoseStrategy.FULL:
        dose = full
        buildup = False
    elif previous_ammonia_n is None:
        dose = min(0.25, full * 0.25)
        buildup = True
    else:
        step = abs(ammonia_n - previous_ammonia_n) * ammonia_ratio
        dose = min(0.5, max(full * 0.1, step))
        buildup = dose < step
    return full, max(0.0, dose), strategy, buildup


def feedback_correction(
    target: float,
    measured_residual: float,
    gain: float = DEFAULT_FEEDBACK_GAIN,
    cap: float = DEFAULT_FEEDBACK_CAP,
    deadband: float = DEFAULT_RESIDUAL_DEADBAND,
) -> float:
    """Signed bounded correction; negative values mean reduce the next setpoint."""

    error = target - measured_residual
    if abs(error) <= deadband:
        return 0.0
    return max(-cap, min(cap, gain * error))


def update_bias(bias: float, correction: float) -> float:
    """Persist feedback as a slowly reusable feedforward correction."""

    return max(-5.0, min(5.0, bias + 0.5 * correction))
