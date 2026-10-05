"""Behavioural tests for the chlorine feedforward/feedback loop."""

from __future__ import annotations

import tempfile
import unittest

from waterplant.chlor import (
    ChlorRoundConflict,
    DoseStrategy,
    Doser,
    base_dose,
    contact_time,
    feedback_correction,
    select_strategy,
)
from waterplant.intake import FlowRepository
from waterplant.store import Store


class ChlorPolicyCase(unittest.TestCase):
    def test_contact_time_prefers_valid_measured_flow_and_falls_back_to_design(self) -> None:
        minutes, source, flow, used = contact_time(1_000.0, 500.0, 1_200.0)
        self.assertAlmostEqual(minutes, 50.0)
        self.assertEqual(source, "measured_flow")
        self.assertEqual(flow, 1_200.0)
        self.assertTrue(used)

        minutes, source, flow, used = contact_time(1_000.0, 500.0, None)
        self.assertEqual(minutes, 120.0)
        self.assertEqual(source, "design_flow")
        self.assertEqual(flow, 500.0)
        self.assertFalse(used)

    def test_dose_includes_ammonia_demand_and_decay_over_contact_time(self) -> None:
        dose = base_dose(target=0.5, ammonia_n=0.1, contact_minutes=60.0, ammonia_ratio=7.6)
        self.assertGreater(dose, 1.26)
        self.assertLess(dose, 1.28)

    def test_water_quality_selects_full_or_small_step_strategy(self) -> None:
        self.assertIs(select_strategy(0.2, 0.1), DoseStrategy.FULL)
        self.assertIs(select_strategy(0.6, 0.1), DoseStrategy.INCREMENTAL)
        self.assertIs(select_strategy(0.2, 0.5), DoseStrategy.INCREMENTAL)

    def test_feedback_is_deadbanded_and_bounded(self) -> None:
        self.assertEqual(feedback_correction(0.5, 0.5), 0.0)
        self.assertAlmostEqual(feedback_correction(0.5, 0.4), 0.08)
        self.assertEqual(feedback_correction(0.5, 3.0, cap=0.5), -0.5)


class ChlorRoundCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        FlowRepository(self.store).persist_flow(1_000.0)
        self.clock = FakeClock(10_000.0)
        self.doser = Doser(self.store, clock=self.clock.now)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_feedback_must_wait_for_contact_time(self) -> None:
        round_state, applied = self.doser.start_round(0.1, request_id="r1")
        self.assertTrue(applied)
        self.assertEqual(round_state.contact_minutes, 60.0)
        with self.assertRaises(ChlorRoundConflict):
            self.doser.report_feedback(0.4, elapsed_minutes=59.0)
        _, feedback, applied = self.doser.report_feedback(0.4, elapsed_minutes=60.0)
        self.assertTrue(applied)
        self.assertGreater(feedback.correction_mg_l, 0.0)
        self.assertGreater(feedback.applied_correction_mg_l, 0.0)
        self.assertIsNone(self.doser.rounds.current())
        self.assertEqual(self.doser.rounds.list_rounds()[0]["status"], "feedback_corrected")

    def test_second_feedforward_requires_previous_residual_confirmation(self) -> None:
        self.doser.start_round(0.1)
        with self.assertRaises(ChlorRoundConflict):
            self.doser.start_round(0.2)

    def test_lost_reading_is_partial_failure_and_recovery_is_idempotent(self) -> None:
        self.doser.start_round(0.1, round_id="round-1", request_id="start-1")
        lost, recorded = self.doser.mark_reading_lost(round_id="round-1", loss_id="loss-1")
        self.assertTrue(recorded)
        self.assertEqual(lost.status, "residual_reading_lost")

        repeated, recorded = self.doser.mark_reading_lost(round_id="round-1", loss_id="loss-1")
        self.assertFalse(recorded)
        self.assertEqual(repeated.loss_id, "loss-1")

        with self.assertRaises(ChlorRoundConflict):
            self.doser.start_round(0.2)
        with self.assertRaises(ChlorRoundConflict):
            self.doser.report_feedback(0.4, elapsed_minutes=60.0)

        recovered, feedback, applied = self.doser.recover_reading(
            0.4,
            elapsed_minutes=60.0,
            round_id="round-1",
            request_id="recovery-1",
        )
        self.assertTrue(applied)
        self.assertEqual(feedback.round_id, "round-1")

        recovered_again, feedback_again, applied_again = self.doser.recover_reading(
            0.9,
            elapsed_minutes=70.0,
            round_id="round-1",
            request_id="recovery-1",
        )
        self.assertFalse(applied_again)
        self.assertEqual(recovered_again.round_id, "round-1")
        self.assertEqual(feedback_again.measured_residual, 0.4)

        kinds = self.doser._auditor.count_by_kind()
        self.assertEqual(kinds["chlorine_reading_lost"], 1)
        self.assertEqual(kinds["chlorine"], 2)

    def test_recovery_without_supplied_request_id_stays_idempotent_for_round(self) -> None:
        self.doser.start_round(0.1)
        round_state, _ = self.doser.mark_reading_lost()
        first, _, applied = self.doser.recover_reading(0.4, elapsed_minutes=60.0)
        self.assertTrue(applied)
        second, _, applied = self.doser.recover_reading(0.9, elapsed_minutes=61.0)
        self.assertFalse(applied)
        self.assertEqual(first.round_id, second.round_id)

    def test_same_feedforward_request_id_is_idempotent(self) -> None:
        first, applied = self.doser.start_round(0.1, request_id="client-1")
        self.assertTrue(applied)
        second, applied = self.doser.start_round(0.9, request_id="client-1")
        self.assertFalse(applied)
        self.assertEqual(first.round_id, second.round_id)
        self.assertEqual(second.ammonia_n, 0.1)

    def test_high_ammonia_uses_small_step_before_retest(self) -> None:
        round_state, _ = self.doser.start_round(0.6)
        plan = round_state.dose_plan()
        self.assertEqual(plan.strategy, DoseStrategy.INCREMENTAL.value)
        self.assertTrue(plan.buildup)
        self.assertEqual(plan.feedback_minutes, 10.0)
        self.assertLessEqual(plan.dose_mg_l, 0.25)
        self.assertGreater(plan.dose_mg_l, 0.0)
        with self.assertRaises(ChlorRoundConflict):
            self.doser.report_feedback(0.4, elapsed_minutes=9.0)
        _, feedback, _ = self.doser.report_feedback(0.4, elapsed_minutes=10.0)
        self.assertGreater(feedback.correction_mg_l, 0.0)

    def test_fast_ammonia_change_after_stable_round_switches_to_small_step(self) -> None:
        first, _ = self.doser.start_round(0.1)
        self.assertEqual(first.dose_plan().strategy, DoseStrategy.FULL.value)
        self.doser.report_feedback(0.5, elapsed_minutes=60.0)
        second, _ = self.doser.start_round(0.4)
        plan = second.dose_plan()
        self.assertEqual(plan.strategy, DoseStrategy.INCREMENTAL.value)
        self.assertLessEqual(plan.dose_mg_l, 0.5)


class FakeClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def now(self) -> float:
        return self.value


if __name__ == "__main__":
    unittest.main()
