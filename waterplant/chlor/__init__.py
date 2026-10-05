"""Chlorine dosing."""

from .doser import CHLORINE, Doser
from .errors import ChlorRoundConflict
from .hydraulics import Hydraulics
from .report import (
    ChlorState,
    DosePlan,
    DoseStrategy,
    FeedbackResult,
    HydraulicsState,
    RoundStatus,
    WaterPolicy,
    base_dose,
    contact_time,
    feedback_correction,
    feedforward_dose,
    select_strategy,
    update_bias,
    validate_ammonia,
    validate_demand,
    validate_elapsed_minutes,
    validate_residual,
)
from .round import RoundRepository, RoundState

__all__ = [
    "CHLORINE",
    "ChlorRoundConflict",
    "ChlorState",
    "DosePlan",
    "DoseStrategy",
    "Doser",
    "FeedbackResult",
    "Hydraulics",
    "HydraulicsState",
    "RoundRepository",
    "RoundState",
    "RoundStatus",
    "WaterPolicy",
    "base_dose",
    "contact_time",
    "feedback_correction",
    "feedforward_dose",
    "select_strategy",
    "update_bias",
    "validate_ammonia",
    "validate_demand",
    "validate_elapsed_minutes",
    "validate_residual",
]
