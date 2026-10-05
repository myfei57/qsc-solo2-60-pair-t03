"""Chlorine dosing."""

from .controller import (
    FEEDBACK_KIND,
    FEEDFORWARD_KIND,
    MISSING_KIND,
    RECOVERY_KIND,
    ChlorinationBusyError,
    ChlorinationController,
)
from .doser import CHLORINE, Doser
from .report import (
    ChlorConfig,
    ChlorinationState,
    ChlorState,
    DoseAction,
    DosingRound,
    ResidualObservation,
    StatusEvent,
    contact_time_minutes,
    dose_rate_kg_h,
    model_demand_mg_l,
    select_water_quality,
    validate_ammonia,
    validate_demand,
    validate_residual,
)

__all__ = [
    "CHLORINE",
    "ChlorConfig",
    "ChlorState",
    "ChlorinationBusyError",
    "ChlorinationController",
    "ChlorinationState",
    "DoseAction",
    "Doser",
    "DosingRound",
    "FEEDBACK_KIND",
    "FEEDFORWARD_KIND",
    "MISSING_KIND",
    "RECOVERY_KIND",
    "ResidualObservation",
    "StatusEvent",
    "contact_time_minutes",
    "dose_rate_kg_h",
    "model_demand_mg_l",
    "select_water_quality",
    "validate_ammonia",
    "validate_demand",
    "validate_residual",
]
