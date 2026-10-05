"""HTTP handlers for every console endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

from waterplant.chlor import (
    validate_demand,
    validate_elapsed_minutes,
    validate_residual,
)
from waterplant.clearwell import validate_level
from waterplant.filter import validate_zone
from waterplant.flow import calibrate_meter, validate_factor
from waterplant.intake import DEFAULT_WINDOW, Sensor, validate_flow
from waterplant.inventory import validate_quantity
from waterplant.ns import Stage, treatment_line
from waterplant.ph import validate_ph
from waterplant.quota import Quota, check_quota, validate_amount
from waterplant.reporting import audit_export, telemetry_export, to_csv
from waterplant.scheduler import validate_threshold
from waterplant.store import export_state

from . import checks, history, ops
from . import describe as describe_module
from . import simulate as simulate_module
from . import snapshot as snapshot_module
from . import telemetry as telemetry_module
from .cycle import run_cycle
from .http import Request, Response, csv_response, json_response, text_response
from .report import text_report
from .routes import route_table
from .system import system_payload
from .version import version_payload

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .server import Server


def health(server: "Server", request: Request) -> Response:
    store = server.runtime.store
    return json_response(
        {"status": "ok", "keys": store.keys(), "size": store.count(), "data": export_state(store)}
    )


def health_checks(server: "Server", request: Request) -> Response:
    return json_response({"checks": checks.run_checks(server.runtime)})


def snapshot(server: "Server", request: Request) -> Response:
    return json_response(snapshot_module.collect(server.runtime))


def describe(server: "Server", request: Request) -> Response:
    return json_response(describe_module.collect(server.runtime))


def report(server: "Server", request: Request) -> Response:
    return text_response(text_report(server.runtime))


def pipeline(server: "Server", request: Request) -> Response:
    line = treatment_line()
    last = line.last()
    return json_response(
        {
            "name": line.name,
            "steps": [step.as_dict() for step in line.steps()],
            "ordered": line.before(Stage.INTAKE, Stage.COAG),
            "last_stage": "" if last is None else last.value,
        }
    )


def catalog(server: "Server", request: Request) -> Response:
    return json_response({"routes": [route.as_dict() for route in route_table()]})


def ops_counters(server: "Server", request: Request) -> Response:
    return json_response(ops.collect(server.runtime).as_dict())


def ops_reset_store(server: "Server", request: Request) -> Response:
    return json_response(ops.reset_store(server.runtime))


def telemetry(server: "Server", request: Request) -> Response:
    return json_response(telemetry_module.collect(server.runtime).as_dict())


def version(server: "Server", request: Request) -> Response:
    return json_response(version_payload())


def system(server: "Server", request: Request) -> Response:
    return json_response(system_payload())


def history_append(server: "Server", request: Request) -> Response:
    return history.append(server.runtime.store, request)


def history_list(server: "Server", request: Request) -> Response:
    return history.listing(server.runtime.store)


def history_clear(server: "Server", request: Request) -> Response:
    return history.clear(server.runtime.store)


def intake_flow(server: "Server", request: Request) -> Response:
    sensor = Sensor.from_payload(request.payload)
    validate_flow(sensor.flow)
    server.runtime.flow_repository.record(sensor)
    server.runtime.trend.record(sensor.flow)
    return json_response({"flow": sensor.flow, "turbidity": sensor.turbidity})


def intake_flow_get(server: "Server", request: Request) -> Response:
    value, present = server.runtime.flow_repository.load_flow()
    return json_response({"flow": value, "ok": present})


def coag_dose(server: "Server", request: Request) -> Response:
    flow = request.float_field("flow")
    validate_flow(flow)
    dose = server.runtime.coag_doser.update_flow_and_dose(flow)
    return json_response({"dose": dose})


def coag_turbidity(server: "Server", request: Request) -> Response:
    dose = server.runtime.sampler.judge(request.float_list("samples"))
    return json_response({"dose": dose})


def coag_ratio(server: "Server", request: Request) -> Response:
    return json_response({"ratio": server.runtime.coag_doser.current_ratio()})


def flow_replace(server: "Server", request: Request) -> Response:
    factor = request.float_field("factor")
    validate_factor(factor)
    meter = calibrate_meter(request.str_field("serial"), factor)
    server.runtime.calibration.replace(meter.factor)
    return json_response({"meter": meter.as_dict()})


def chlor_target(server: "Server", request: Request) -> Response:
    if "target" in request.payload:
        target = request.float_field("target")
        validate_residual(target, "target")
        saved = server.runtime.chlor_doser.set_target(target)
        return json_response({"target": saved, "mode": "residual_target"})
    demand = request.float_field("demand")
    validate_demand(demand)
    target = server.runtime.well.update_residual_demand(demand)
    return json_response({"target": target, "mode": "demand"})


def chlor_config(server: "Server", request: Request) -> Response:
    doser = server.runtime.chlor_doser
    if request.payload:
        if "design_flow" in request.payload or "effective_volume" in request.payload:
            current = doser.hydraulics.state()
            doser.hydraulics.configure(
                request.float_field("design_flow", current.design_flow),
                request.float_field("effective_volume", current.effective_volume),
            )
        if "ammonia_ratio" in request.payload:
            doser.set_ammonia_ratio(request.float_field("ammonia_ratio"))
        if "feedback_gain" in request.payload:
            doser.set_feedback_gain(request.float_field("feedback_gain"))
        if "feedback_cap" in request.payload:
            doser.set_feedback_cap(request.float_field("feedback_cap"))
        if "residual_deadband" in request.payload:
            doser.set_residual_deadband(request.float_field("residual_deadband"))
        if "residual_decay_rate" in request.payload:
            doser.set_residual_decay_rate(request.float_field("residual_decay_rate"))
        if "ammonia_incremental_threshold" in request.payload or "ammonia_change_threshold" in request.payload:
            policy = doser.policy()
            doser.set_water_policy(
                request.float_field(
                    "ammonia_incremental_threshold",
                    policy.ammonia_incremental_threshold,
                ),
                request.float_field(
                    "ammonia_change_threshold",
                    policy.ammonia_change_threshold,
                ),
            )
    hydraulics = doser.hydraulics.state(doser.measured_flow())
    return json_response(
        {
            "hydraulics": hydraulics.as_dict(),
            "policy": doser.policy().as_dict(),
            "ammonia_ratio": doser.ammonia_ratio(),
            "feedback_gain": doser.feedback_gain(),
            "feedback_cap": doser.feedback_cap(),
            "residual_deadband": doser.residual_deadband(),
            "residual_decay_rate": doser.residual_decay_rate(),
            "learned_bias": doser.learned_bias(),
        }
    )


def chlor_dose(server: "Server", request: Request) -> Response:
    doser = server.runtime.chlor_doser
    ammonia = request.float_field("ammonia_n", -1.0)
    request_id = request.str_field("request_id")
    round_id = request.str_field("round_id")
    round_state, applied = doser.start_round(
        None if ammonia < 0 else ammonia,
        request_id=request_id,
        round_id=round_id,
    )
    plan = round_state.dose_plan()
    return json_response(
        {
            "applied": applied,
            "round_id": round_state.round_id,
            "request_id": round_state.request_id,
            "status": round_state.status,
            "plan": plan.as_dict(),
            "due_at": round_state.due_at,
        }
    )


def chlor_round(server: "Server", request: Request) -> Response:
    doser = server.runtime.chlor_doser
    current = doser.rounds.current()
    ledger = doser.rounds.ledger()
    return json_response(
        {
            "current": None if current is None else current.as_dict(),
            "count": ledger.count,
            "rounds": list(reversed(ledger.rounds)),
        }
    )


def chlor_residual_lost(server: "Server", request: Request) -> Response:
    round_state, recorded = server.runtime.chlor_doser.mark_reading_lost(
        round_id=request.str_field("round_id"),
        loss_id=request.str_field("loss_id"),
        reason=request.str_field("reason", "residual reading unavailable"),
    )
    return json_response(
        {"recorded": recorded, "status": round_state.status, "round_id": round_state.round_id}
    )


def chlor_residual(server: "Server", request: Request) -> Response:
    doser = server.runtime.chlor_doser
    elapsed_raw = request.payload.get("elapsed_minutes")
    elapsed = None if elapsed_raw is None else request.float_field("elapsed_minutes")
    if elapsed is not None:
        validate_elapsed_minutes(elapsed)
    round_state, feedback, applied = doser.report_feedback(
        request.float_field("measured_residual"),
        elapsed_minutes=elapsed,
        round_id=request.str_field("round_id"),
        reading_id=request.str_field("reading_id"),
        feedback_id=request.str_field("feedback_id"),
        request_id=request.str_field("request_id"),
    )
    return json_response(
        {
            "applied": applied,
            "status": round_state.status,
            "round_id": round_state.round_id,
            "feedback": feedback.as_dict(),
        }
    )


def chlor_residual_recover(server: "Server", request: Request) -> Response:
    doser = server.runtime.chlor_doser
    elapsed_raw = request.payload.get("elapsed_minutes")
    elapsed = None if elapsed_raw is None else request.float_field("elapsed_minutes")
    if elapsed is not None:
        validate_elapsed_minutes(elapsed)
    round_state, feedback, applied = doser.recover_reading(
        request.float_field("measured_residual"),
        elapsed_minutes=elapsed,
        round_id=request.str_field("round_id"),
        reading_id=request.str_field("reading_id"),
        request_id=request.str_field("request_id"),
    )
    return json_response(
        {
            "applied": applied,
            "status": round_state.status,
            "round_id": round_state.round_id,
            "feedback": feedback.as_dict(),
        }
    )


def clearwell_level(server: "Server", request: Request) -> Response:
    target = request.float_field("target")
    validate_level(target)
    runtime = server.runtime
    minimum = runtime.well.adjust_level(target, runtime.inlet, runtime.outlet)
    return json_response({"min_level": minimum, "level": runtime.well.level()})


def filter_add(server: "Server", request: Request) -> Response:
    zone = request.int_field("zone")
    validate_zone(zone)
    bed = server.runtime.bank.add_bed(
        request.str_field("id"), zone, request.float_field("load")
    )
    return json_response({"id": bed.id, "zone": bed.zone, "load": bed.load})


def filter_close(server: "Server", request: Request) -> Response:
    bed_id = request.str_field("id")
    server.runtime.bank.close(bed_id)
    return json_response({"closed": bed_id})


def filter_open(server: "Server", request: Request) -> Response:
    bed_id = request.str_field("id")
    server.runtime.bank.open(bed_id)
    return json_response({"opened": bed_id})


def filter_renumber(server: "Server", request: Request) -> Response:
    zone = request.int_field("zone")
    validate_zone(zone)
    bed_id = request.str_field("id")
    server.runtime.bank.renumber(bed_id, zone)
    return json_response({"renumbered": bed_id, "zone": zone})


def filter_load(server: "Server", request: Request) -> Response:
    bed_id = request.str_field("id")
    load = request.float_field("load")
    if load < 0:
        raise ValueError("load must be non-negative")
    server.runtime.bank.set_load(bed_id, load)
    return json_response({"id": bed_id, "load": load})


def filter_reset(server: "Server", request: Request) -> Response:
    server.runtime.bank.reset_closed()
    return json_response({"reset": True})


def filter_remove(server: "Server", request: Request) -> Response:
    bed_id = request.str_field("id")
    server.runtime.bank.remove_bed(bed_id)
    return json_response({"removed": bed_id})


def filter_zone(server: "Server", request: Request) -> Response:
    bed_id = request.str_field("id")
    zone, present = server.runtime.bank.bed_zone(bed_id)
    return json_response({"id": bed_id, "zone": zone, "ok": present})


def backwash_start(server: "Server", request: Request) -> Response:
    bed_id = request.str_field("id")
    server.runtime.backwash.start(bed_id)
    return json_response({"started": bed_id})


def backwash_order(server: "Server", request: Request) -> Response:
    runtime = server.runtime
    order = runtime.backwash.order_rotation()
    return json_response({"order": order, "on_duty": runtime.bank.on_duty() or ""})


def backwash_select(server: "Server", request: Request) -> Response:
    zone = request.int_field("zone")
    bed_id = server.runtime.backwash.select(zone)
    return json_response({"bed": bed_id, "zone": zone})


def backwash_enqueue(server: "Server", request: Request) -> Response:
    bed_id = request.str_field("id")
    server.runtime.backwash.enqueue(bed_id)
    return json_response({"enqueued": bed_id})


def backwash_recover(server: "Server", request: Request) -> Response:
    server.runtime.backwash.recover()
    return json_response({"recovered": True})


def backwash_replay(server: "Server", request: Request) -> Response:
    return json_response({"replayed": server.runtime.backwash.replay()})


def quota_add(server: "Server", request: Request) -> Response:
    amount = request.float_field("amount")
    validate_amount(amount)
    return json_response({"value": server.runtime.accumulator.add(amount)})


def quota_get(server: "Server", request: Request) -> Response:
    return json_response({"value": server.runtime.accumulator.value()})


def quota_check(server: "Server", request: Request) -> Response:
    quota = Quota(chemical=request.str_field("chemical"), limit=request.float_field("limit"))
    remaining, ok = check_quota(quota.chemical, request.float_field("used"), quota.limit)
    return json_response({"remaining": remaining, "ok": ok})


def audit_list(server: "Server", request: Request) -> Response:
    return json_response({"entries": [entry.as_dict() for entry in server.runtime.auditor.entries()]})


def audit_summary(server: "Server", request: Request) -> Response:
    auditor = server.runtime.auditor
    return json_response({"count": auditor.count(), "by_kind": auditor.count_by_kind()})


def audit_filter(server: "Server", request: Request) -> Response:
    entries = server.runtime.auditor.filter(request.str_field("kind"))
    return json_response({"entries": [entry.as_dict() for entry in entries]})


def cycle(server: "Server", request: Request) -> Response:
    return run_cycle(server.runtime, request)


def simulate(server: "Server", request: Request) -> Response:
    return simulate_module.run_simulation(server.runtime, request)


def ph_state(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.stabilizer.state().as_dict())


def ph_read(server: "Server", request: Request) -> Response:
    value = request.float_field("value")
    validate_ph(value)
    return json_response(server.runtime.stabilizer.read(value).as_dict())


def ph_verdict(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.stabilizer.stabilize().as_dict())


def schedule_state(server: "Server", request: Request) -> Response:
    runtime = server.runtime
    return json_response(runtime.scheduler.state(runtime.bank).as_dict())


def schedule_threshold(server: "Server", request: Request) -> Response:
    value = request.float_field("threshold")
    validate_threshold(value)
    return json_response({"threshold": server.runtime.scheduler.set_threshold(value)})


def schedule_plan(server: "Server", request: Request) -> Response:
    runtime = server.runtime
    entries = runtime.scheduler.plan(runtime.bank)
    return json_response(
        {"entries": [entry.as_dict() for entry in entries], "due": runtime.scheduler.due(runtime.bank)}
    )


def intake_trend(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.trend.stats().as_dict())


def intake_trend_window(server: "Server", request: Request) -> Response:
    value = request.int_field("window", DEFAULT_WINDOW)
    server.runtime.trend.set_window(value)
    return json_response({"window": server.runtime.trend.window()})


def intake_trend_reset(server: "Server", request: Request) -> Response:
    server.runtime.trend.reset()
    return json_response({"reset": True})


def audit_totals(server: "Server", request: Request) -> Response:
    return json_response({"totals": server.runtime.auditor.totals()})


def inventory_state(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.inventory.state().as_dict())


def inventory_receive(server: "Server", request: Request) -> Response:
    amount = request.float_field("amount")
    validate_quantity(amount)
    lot = server.runtime.inventory.receive(
        request.str_field("chemical"), request.str_field("lot_id"), amount
    )
    return json_response({"lot": lot.as_dict()})


def inventory_consume(server: "Server", request: Request) -> Response:
    amount = request.float_field("amount")
    validate_quantity(amount)
    chemical = request.str_field("chemical")
    consumed = server.runtime.inventory.consume(chemical, amount)
    return json_response(
        {"consumed": consumed, "balance": server.runtime.inventory.balance(chemical)}
    )


def inventory_reorder_level(server: "Server", request: Request) -> Response:
    level = request.float_field("level")
    validate_quantity(level, "level")
    return json_response({"reorder_level": server.runtime.inventory.set_reorder_level(level)})


def metrics(server: "Server", request: Request) -> Response:
    return json_response(server.metrics.snapshot().as_dict())


def metrics_reset(server: "Server", request: Request) -> Response:
    server.metrics.reset()
    return json_response({"reset": True})


def export_audit(server: "Server", request: Request) -> Response:
    columns, rows = audit_export(server.runtime.auditor)
    return csv_response(to_csv(columns, rows))


def export_telemetry(server: "Server", request: Request) -> Response:
    columns, rows = telemetry_export(telemetry_module.collect(server.runtime))
    return csv_response(to_csv(columns, rows))
