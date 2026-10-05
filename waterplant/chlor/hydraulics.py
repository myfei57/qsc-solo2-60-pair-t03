"""Clear-well contact-time configuration."""

from __future__ import annotations

from waterplant.store.epoch import load_float, save_float
from waterplant.store.store import Store

from .report import (
    DEFAULT_DESIGN_FLOW,
    DEFAULT_EFFECTIVE_VOLUME,
    HydraulicsState,
    contact_time,
    validate_positive,
)

DESIGN_FLOW_KEY = "chlor:design-flow"
EFFECTIVE_VOLUME_KEY = "chlor:effective-volume"
MAX_FLOW = 1_000_000.0
MAX_VOLUME = 1_000_000.0


class Hydraulics:
    """Persists the effective volume/design flow used for contact time."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def design_flow(self) -> float:
        value, present = load_float(self._store, DESIGN_FLOW_KEY)
        return value if present else DEFAULT_DESIGN_FLOW

    def effective_volume(self) -> float:
        value, present = load_float(self._store, EFFECTIVE_VOLUME_KEY)
        return value if present else DEFAULT_EFFECTIVE_VOLUME

    def configure(self, design_flow: float, effective_volume: float) -> None:
        validate_positive(design_flow, "design_flow", MAX_FLOW)
        validate_positive(effective_volume, "effective_volume", MAX_VOLUME)
        save_float(self._store, DESIGN_FLOW_KEY, design_flow)
        save_float(self._store, EFFECTIVE_VOLUME_KEY, effective_volume)

    def calculate(
        self,
        measured_flow: float | None = None,
        minimum_flow_ratio: float = 0.2,
        maximum_flow_ratio: float = 1.5,
    ) -> HydraulicsState:
        """Return the policy-locked contact time.

        A calibrated measured flow between 20% and 150% of design flow is used.
        Missing, zero or implausible PLC values fall back to the design value;
        the selected source is returned so every round and audit row agrees.
        """

        design = self.design_flow()
        valid = (
            measured_flow is not None
            and design * minimum_flow_ratio <= measured_flow <= design * maximum_flow_ratio
        )
        minutes, source, flow, used = contact_time(
            self.effective_volume(), design, measured_flow if valid else None
        )
        return HydraulicsState(
            design_flow=design,
            effective_volume=self.effective_volume(),
            measured_flow=flow,
            measured_flow_used=used,
            contact_minutes=minutes,
            contact_source=source,
        )

    def state(self, measured_flow: float | None = None) -> HydraulicsState:
        return self.calculate(measured_flow)
