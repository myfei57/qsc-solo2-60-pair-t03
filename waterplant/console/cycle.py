"""One full treatment control cycle."""

from __future__ import annotations

from waterplant.chlor import validate_ammonia
from waterplant.clearwell import validate_level
from waterplant.filter import validate_zone
from waterplant.flow import validate_factor
from waterplant.intake import Sensor, mix, validate_flow
from waterplant.quota import validate_amount

from .http import Request, Response, json_response
from .runtime import Runtime


def run_cycle(rt: Runtime, request: Request) -> Response:
    """Walk the whole line once, in production order."""

    flow = request.float_field("flow")
    samples = request.float_list("samples")
    demand = request.float_field("demand")
    ammonia_n = request.float_field("ammonia_n", 0.0)
    level = request.float_field("level")
    bed_id = request.str_field("bed_id")
    zone = request.int_field("zone")
    amount = request.float_field("amount")

    validate_flow(flow)
    validate_factor(rt.calibration.current())
    validate_zone(zone)
    validate_amount(amount)
    validate_level(level)
    validate_ammonia(ammonia_n)

    rt.flow_repository.record(Sensor(flow=flow, turbidity=mix(samples)))
    rt.trend.record(flow)
    coag_dose = rt.coag_doser.update_flow_and_dose(flow)
    turb_dose = rt.sampler.judge(samples)
    ph_verdict = rt.stabilizer.stabilize()
    rt.well.update_residual_demand(demand)
    chlor_plan = rt.chlor_doser.plan_dose(ammonia_n)
    chlor_dose = chlor_plan.dose_mg_l if ph_verdict.stable else 0.0
    if ph_verdict.stable:
        rt.auditor.record_event(
            kind="chlorine_cycle_plan",
            detail=f"planned={chlor_dose:.4f}mg/L",
            amount=chlor_dose,
            phase="cycle_feedforward",
            payload={**chlor_plan.as_dict(), "mass_kg_h": chlor_plan.dose_kg_h},
        )
    min_level = rt.well.adjust_level(level, rt.inlet, rt.outlet)
    rotation = rt.backwash.order_rotation()
    duty = rt.bank.on_duty()
    if bed_id:
        rt.backwash.start(bed_id)
    quota_value = rt.accumulator.add(amount)

    return json_response(
        {
            "coag_dose": coag_dose,
            "turb_dose": turb_dose,
            "chlor_dose": chlor_dose,
            "min_level": min_level,
            "level": rt.well.level(),
            "quota": quota_value,
            "rotation": rotation,
            "on_duty": duty or "",
            "backwash": bed_id,
            "drains": rt.backwash.drain_count(),
            "audit_count": rt.auditor.count(),
            "ph_stable": ph_verdict.stable,
            "ph_adjustment": ph_verdict.adjustment,
        }
    )
