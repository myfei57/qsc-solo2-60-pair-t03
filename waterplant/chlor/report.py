"""Chlorine projections and validation rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

WaterQuality = Literal["stable", "variable"]
ContactBasis = Literal["measured_flow", "design_fallback"]
RoundStatus = Literal["waiting_residual", "residual_missing", "closed"]
DosePhase = Literal["feedforward", "feedback"]


@dataclass(frozen=True)
class ChlorState:
    """Residual target together with the legacy instantaneous dose."""

    target: float
    dose: float

    def as_dict(self) -> dict[str, float]:
        return {"target": self.target, "dose": self.dose}


@dataclass(frozen=True)
class ChlorConfig:
    """Fixed policy and clear-well model parameters."""

    volume_m3: float = 1000.0
    design_hrt_minutes: float = 30.0
    design_flow_m3h: float = 2000.0
    decay_mg_per_l_h: float = 0.03
    breakpoint_ratio: float = 7.6
    ammonia_threshold_mg_l: float = 0.05
    ammonia_change_mg_l: float = 0.10
    stable_feedforward_fraction: float = 1.0
    variable_feedforward_fraction: float = 0.65
    feedback_fraction: float = 0.50
    trim_fraction: float = 0.25
    full_step_feedback_rounds: int = 1
    small_step_feedback_rounds: int = 3
    residual_tolerance_mg_l: float = 0.05
    bias_learning_gain: float = 0.5
    max_bias_mg_l: float = 5.0
    mode_policy: str = "auto_by_water_quality"
    contact_time_policy: str = "measured_flow_with_design_fallback"

    def as_dict(self) -> dict[str, object]:
        return {
            "volume_m3": self.volume_m3,
            "design_hrt_minutes": self.design_hrt_minutes,
            "design_flow_m3h": self.design_flow_m3h,
            "decay_mg_per_l_h": self.decay_mg_per_l_h,
            "breakpoint_ratio": self.breakpoint_ratio,
            "ammonia_threshold_mg_l": self.ammonia_threshold_mg_l,
            "ammonia_change_mg_l": self.ammonia_change_mg_l,
            "stable_feedforward_fraction": self.stable_feedforward_fraction,
            "variable_feedforward_fraction": self.variable_feedforward_fraction,
            "feedback_fraction": self.feedback_fraction,
            "trim_fraction": self.trim_fraction,
            "full_step_feedback_rounds": self.full_step_feedback_rounds,
            "small_step_feedback_rounds": self.small_step_feedback_rounds,
            "residual_tolerance_mg_l": self.residual_tolerance_mg_l,
            "bias_learning_gain": self.bias_learning_gain,
            "max_bias_mg_l": self.max_bias_mg_l,
            "mode_policy": self.mode_policy,
            "contact_time_policy": self.contact_time_policy,
        }


@dataclass(frozen=True)
class DoseAction:
    """One feedforward or feedback command for a parcel."""

    sequence: int
    phase: DosePhase
    at: float
    due_at: float
    target_residual_mg_l: float
    ammonia_n_mg_l: float
    flow_m3h: float
    contact_time_minutes: float
    contact_basis: ContactBasis
    model_demand_mg_l: float
    dose_concentration_mg_l: float
    dose_rate_kg_h: float
    cumulative_concentration_mg_l: float
    idempotent: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "phase": self.phase,
            "at": self.at,
            "due_at": self.due_at,
            "target_residual_mg_l": self.target_residual_mg_l,
            "ammonia_n_mg_l": self.ammonia_n_mg_l,
            "flow_m3h": self.flow_m3h,
            "contact_time_minutes": self.contact_time_minutes,
            "contact_basis": self.contact_basis,
            "model_demand_mg_l": self.model_demand_mg_l,
            "dose_concentration_mg_l": self.dose_concentration_mg_l,
            "dose_rate_kg_h": self.dose_rate_kg_h,
            "cumulative_concentration_mg_l": self.cumulative_concentration_mg_l,
            "idempotent": self.idempotent,
        }


@dataclass(frozen=True)
class ResidualObservation:
    """Actual residual measured after the hydraulic delay."""

    reading_id: str
    request_id: str
    at: float
    residual_mg_l: float
    expected_after_action: int
    error_mg_l: float
    recovered: bool = False
    idempotent: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "reading_id": self.reading_id,
            "request_id": self.request_id,
            "at": self.at,
            "residual_mg_l": self.residual_mg_l,
            "expected_after_action": self.expected_after_action,
            "error_mg_l": self.error_mg_l,
            "recovered": self.recovered,
            "idempotent": self.idempotent,
        }


@dataclass(frozen=True)
class StatusEvent:
    """A partial-failure or recovery event in the active round."""

    kind: str
    at: float
    request_id: str
    reading_id: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "at": self.at,
            "request_id": self.request_id,
            "reading_id": self.reading_id,
        }


@dataclass(frozen=True)
class DosingRound:
    """The complete feedforward/feedback trail for one parcel."""

    round_id: str
    request_id: str
    started_at: float
    target_residual_mg_l: float
    ammonia_n_mg_l: float
    previous_ammonia_n_mg_l: float | None
    water_quality: WaterQuality
    strategy: str
    feedforward_fraction: float
    max_feedback_rounds: int
    bias_before_mg_l: float
    status: RoundStatus
    actions: list[DoseAction] = field(default_factory=list)
    observations: list[ResidualObservation] = field(default_factory=list)
    events: list[StatusEvent] = field(default_factory=list)
    due_at: float = 0.0
    first_error_mg_l: float | None = None
    final_error_mg_l: float | None = None
    final_residual_mg_l: float | None = None
    closed_at: float | None = None
    bias_after_mg_l: float | None = None
    closed_reason: str = ""
    missing_since: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "round_id": self.round_id,
            "request_id": self.request_id,
            "started_at": self.started_at,
            "target_residual_mg_l": self.target_residual_mg_l,
            "ammonia_n_mg_l": self.ammonia_n_mg_l,
            "previous_ammonia_n_mg_l": self.previous_ammonia_n_mg_l,
            "water_quality": self.water_quality,
            "strategy": self.strategy,
            "feedforward_fraction": self.feedforward_fraction,
            "max_feedback_rounds": self.max_feedback_rounds,
            "bias_before_mg_l": self.bias_before_mg_l,
            "status": self.status,
            "actions": [action.as_dict() for action in self.actions],
            "observations": [observation.as_dict() for observation in self.observations],
            "events": [event.as_dict() for event in self.events],
            "due_at": self.due_at,
            "first_error_mg_l": self.first_error_mg_l,
            "final_error_mg_l": self.final_error_mg_l,
            "final_residual_mg_l": self.final_residual_mg_l,
            "closed_at": self.closed_at,
            "bias_after_mg_l": self.bias_after_mg_l,
            "closed_reason": self.closed_reason,
            "missing_since": self.missing_since,
        }


@dataclass(frozen=True)
class ChlorinationState:
    """Console projection for the closed-loop chlorine controller."""

    config: ChlorConfig
    active_round: DosingRound | None
    last_round: DosingRound | None
    rounds: list[DosingRound]
    learned_bias_mg_l: float
    last_ammonia_n_mg_l: float | None
    processed_requests: dict[str, dict[str, object]]
    round_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "config": self.config.as_dict(),
            "active_round": None if self.active_round is None else self.active_round.as_dict(),
            "last_round": None if self.last_round is None else self.last_round.as_dict(),
            "rounds": [round_item.as_dict() for round_item in self.rounds],
            "learned_bias_mg_l": self.learned_bias_mg_l,
            "last_ammonia_n_mg_l": self.last_ammonia_n_mg_l,
            "processed_requests": self.processed_requests,
            "round_count": self.round_count,
        }


def validate_demand(demand: float) -> None:
    """Reject residual demands outside the supported range."""

    if demand < 0:
        raise ValueError("demand must be non-negative")
    if demand > 1000:
        raise ValueError("demand exceeds the supported range")


def validate_residual(value: float, name: str = "residual") -> None:
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    if value > 10:
        raise ValueError(f"{name} exceeds the supported range")


def validate_ammonia(value: float) -> None:
    if value < 0:
        raise ValueError("ammonia nitrogen must be non-negative")
    if value > 100:
        raise ValueError("ammonia nitrogen exceeds the supported range")


def validate_positive(value: float, name: str, maximum: float) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    if value > maximum:
        raise ValueError(f"{name} exceeds the supported range")


def validate_fraction(value: float, name: str) -> None:
    if value <= 0 or value > 1:
        raise ValueError(f"{name} must be greater than zero and at most one")


def select_water_quality(
    ammonia_n_mg_l: float,
    previous_ammonia_n_mg_l: float | None,
    config: ChlorConfig,
) -> WaterQuality:
    """Determine dosing style from incoming ammonia and its change."""

    changed = (
        previous_ammonia_n_mg_l is not None
        and abs(ammonia_n_mg_l - previous_ammonia_n_mg_l)
        >= config.ammonia_change_mg_l
    )
    if ammonia_n_mg_l > config.ammonia_threshold_mg_l or changed:
        return "variable"
    return "stable"


def contact_time_minutes(
    flow_m3h: float, config: ChlorConfig
) -> tuple[float, ContactBasis, float]:
    """Use measured-flow HRT; the design value is only a no-flow fallback."""

    if flow_m3h > 0:
        return config.volume_m3 * 60.0 / flow_m3h, "measured_flow", flow_m3h
    return config.design_hrt_minutes, "design_fallback", config.design_flow_m3h


def model_demand_mg_l(
    target_residual_mg_l: float,
    ammonia_n_mg_l: float,
    contact_time_minutes: float,
    config: ChlorConfig,
    bias_mg_l: float = 0.0,
) -> float:
    """Residual target plus breakpoint, bulk decay and learned model bias."""

    demand = (
        target_residual_mg_l
        + config.breakpoint_ratio * ammonia_n_mg_l
        + config.decay_mg_per_l_h * contact_time_minutes / 60.0
        + bias_mg_l
    )
    return max(0.0, demand)


def dose_rate_kg_h(dose_concentration_mg_l: float, flow_m3h: float) -> float:
    """mg/L multiplied by m3/h is numerically kg/h."""

    return max(0.0, dose_concentration_mg_l * flow_m3h)
