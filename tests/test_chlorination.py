"""Behavioural tests for closed-loop chlorine dosing."""

from __future__ import annotations

import tempfile
import unittest

from waterplant.audit import Auditor
from waterplant.chlor import (
    ChlorinationBusyError,
    ChlorinationController,
    contact_time_minutes,
    model_demand_mg_l,
    select_water_quality,
)
from waterplant.chlor.report import ChlorConfig
from waterplant.intake import FlowRepository
from waterplant.store import Store


class FakeClock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def advance(self, seconds: float) -> float:
        self.value += seconds
        return self.value

    def __call__(self) -> float:
        return self.value


class ChlorinationCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.clock = FakeClock()
        FlowRepository(self.store).persist_flow(2000.0)
        self.controller = ChlorinationController(self.store, clock=self.clock)
        self.auditor = Auditor(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_measured_contact_time_governs_the_model(self) -> None:
        config = ChlorConfig()
        hrt, basis, flow = contact_time_minutes(2000.0, config)
        self.assertEqual(hrt, 30.0)
        self.assertEqual(basis, "measured_flow")
        self.assertEqual(flow, 2000.0)
        fallback_h, fallback_basis, fallback_flow = contact_time_minutes(0.0, config)
        self.assertEqual((fallback_h, fallback_basis, fallback_flow), (30.0, "design_fallback", 2000.0))
        demand = model_demand_mg_l(0.5, 0.0, 30.0, config)
        self.assertAlmostEqual(demand, 0.515)
        self.assertEqual(select_water_quality(0.01, 0.0, config), "stable")
        self.assertEqual(select_water_quality(0.10, 0.0, config), "variable")

    def test_stable_water_feedforward_once_then_feedback_and_learns_bias(self) -> None:
        started = self.controller.start_round(0.0, "req-1", target_residual_mg_l=0.5, flow_m3h=2000.0)
        round_item = started["round"]
        action = started["action"]
        self.assertFalse(started["idempotent"])
        self.assertEqual(round_item["status"], "waiting_residual")
        self.assertEqual(round_item["water_quality"], "stable")
        self.assertEqual(round_item["strategy"], "feedforward_full_then_feedback")
        self.assertEqual(action["phase"], "feedforward")
        self.assertEqual(action["contact_basis"], "measured_flow")
        self.assertAlmostEqual(action["dose_concentration_mg_l"], 0.515)
        self.assertAlmostEqual(action["dose_rate_kg_h"], 1030.0)

        with self.assertRaises(ChlorinationBusyError):
            self.controller.start_round(0.0, "req-blocked", target_residual_mg_l=0.5, flow_m3h=2000.0)
        self.clock.advance(20 * 60)
        with self.assertRaises(ChlorinationBusyError):
            self.controller.observe_residual(0.30, "reading-1", "req-2")

        self.clock.advance(10 * 60 + 1)
        feedback = self.controller.observe_residual(0.30, "reading-1", "req-2")
        self.assertFalse(feedback["closed"])
        self.assertEqual(feedback["action"]["phase"], "feedback")
        self.assertAlmostEqual(feedback["action"]["dose_concentration_mg_l"], 0.10)
        self.assertAlmostEqual(feedback["action"]["dose_rate_kg_h"], 200.0)
        self.assertEqual(len(feedback["round"]["actions"]), 2)

        self.clock.advance(30 * 60 + 1)
        closed = self.controller.observe_residual(0.51, "reading-2", "req-3")
        self.assertTrue(closed["closed"])
        self.assertEqual(closed["round"]["status"], "closed")
        self.assertEqual(closed["round"]["closed_reason"], "residual_in_tolerance")
        self.assertAlmostEqual(closed["round"]["bias_after_mg_l"], 0.10)

        next_round = self.controller.start_round(0.0, "req-4", target_residual_mg_l=0.5, flow_m3h=2000.0)
        self.assertAlmostEqual(next_round["action"]["model_demand_mg_l"], 0.615)
        kinds = [entry.kind for entry in self.auditor.entries()]
        self.assertEqual(kinds, ["chlorine-feedforward", "chlorine-feedback", "chlorine-feedforward"])

    def test_variable_water_uses_small_steps_and_remeasures_each_delay(self) -> None:
        started = self.controller.start_round(0.10, "req-1", target_residual_mg_l=0.5, flow_m3h=2000.0)
        round_item = started["round"]
        self.assertEqual(round_item["water_quality"], "variable")
        self.assertEqual(round_item["strategy"], "small_steps_remeasure_after_each_hydraulic_delay")
        self.assertEqual(round_item["max_feedback_rounds"], 3)
        self.assertAlmostEqual(started["action"]["dose_concentration_mg_l"], 1.275 * 0.65)

        for index, reading in enumerate(("r1", "r2", "r3"), start=1):
            self.clock.advance(30 * 60 + 1)
            result = self.controller.observe_residual(0.30, reading, f"obs-{index}")
            self.assertFalse(result["closed"])
            self.assertEqual(len(result["round"]["actions"]), index + 1)
            self.assertEqual(result["action"]["phase"], "feedback")
            self.assertAlmostEqual(result["action"]["dose_concentration_mg_l"], 0.05)

        self.clock.advance(30 * 60 + 1)
        result = self.controller.observe_residual(0.30, "r4", "obs-4")
        self.assertTrue(result["closed"])
        self.assertEqual(result["round"]["closed_reason"], "feedback_round_limit")
        self.assertEqual(len(result["round"]["actions"]), 4)

    def test_high_residual_closes_without_negative_dose(self) -> None:
        self.controller.start_round(0.0, "req-1", target_residual_mg_l=0.5, flow_m3h=2000.0)
        self.clock.advance(30 * 60 + 1)
        result = self.controller.observe_residual(0.80, "reading-1", "req-2")
        self.assertTrue(result["closed"])
        self.assertEqual(result["round"]["closed_reason"], "residual_high_no_negative_dose")
        self.assertIsNone(result["action"])
        self.assertEqual(len(result["round"]["actions"]), 1)

    def test_missing_reading_is_partial_failure_recoverable_and_idempotent(self) -> None:
        self.controller.start_round(0.0, "req-start", target_residual_mg_l=0.5, flow_m3h=2000.0)
        missing = self.controller.report_missing_residual("req-missing")
        self.assertFalse(missing["idempotent"])
        self.assertEqual(missing["round"]["status"], "residual_missing")
        repeated_missing = self.controller.report_missing_residual("req-missing")
        self.assertTrue(repeated_missing["idempotent"])

        self.clock.advance(30 * 60 + 1)
        with self.assertRaises(ChlorinationBusyError):
            self.controller.observe_residual(0.5, "reading-1", "req-normal")
        recovered = self.controller.recover_residual(0.5, "reading-1", "req-recover")
        self.assertTrue(recovered["closed"])
        self.assertTrue(recovered["observation"]["recovered"])
        duplicate = self.controller.recover_residual(0.5, "reading-1", "req-recover")
        self.assertTrue(duplicate["idempotent"])
        self.assertTrue(duplicate["closed"])

        self.controller.start_round(0.0, "req-next", target_residual_mg_l=0.5, flow_m3h=2000.0)
        kinds = [entry.kind for entry in self.auditor.entries()]
        self.assertIn("chlorine-residual-missing", kinds)
        self.assertIn("chlorine-residual-recovered", kinds)

    def test_repeated_start_request_is_idempotent(self) -> None:
        first = self.controller.start_round(0.0, "same-req", target_residual_mg_l=0.5, flow_m3h=2000.0)
        second = self.controller.start_round(0.0, "same-req", target_residual_mg_l=0.9, flow_m3h=100.0)
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["round"]["round_id"], first["round"]["round_id"])
        self.assertEqual(second["action"]["dose_rate_kg_h"], first["action"]["dose_rate_kg_h"])
        self.assertEqual(len([e for e in self.auditor.entries() if e.kind == "chlorine-feedforward"]), 1)

    def test_zero_flow_uses_design_contact_time_fallback(self) -> None:
        started = self.controller.start_round(0.0, "req-fallback", target_residual_mg_l=0.5, flow_m3h=0.0)
        action = started["action"]
        self.assertEqual(action["contact_basis"], "design_fallback")
        self.assertEqual(action["contact_time_minutes"], 30.0)
        self.assertEqual(action["flow_m3h"], 2000.0)


if __name__ == "__main__":
    unittest.main()
