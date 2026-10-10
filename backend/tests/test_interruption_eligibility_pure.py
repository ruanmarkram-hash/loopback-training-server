"""Pure real-module seams; run directly with unittest, without database conftest."""
import sys
import unittest
from pathlib import Path
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as Row

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.coaching_policy import deterministic_review
from app.coaching_reports import athlete_reports

NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


def fixture():
    queues = [Row(id=str(i), user_id="actor", activity_type="running", status="skipped",
                  scheduled_date=NOW-timedelta(days=i+1), workout_data={
                      "activityType": "running", "singleGoal": {
                          "type": "distance", "unit": "meters", "value": 5000}}, title="Synthetic")
              for i in range(3)]
    feedback = [Row(workout_id=q.id, user_id=q.user_id, scheduled_date=q.scheduled_date,
                    reason="busy", action="skip", dismissed=False, new_date=None) for q in queues]
    workouts = [Row(id="recent", activity_type="running", start_date=NOW-timedelta(hours=2),
                    total_distance=5000, duration=1800, effort_score=4, data={})]
    return workouts, queues, feedback


class InterruptionEligibilityTests(unittest.TestCase):
    def decision(self, workouts, queues, feedback):
        return deterministic_review(workouts, [], feedback, queues, NOW)

    def test_future_skips_do_not_trigger_interruption(self):
        workouts, queues, feedback = fixture()
        self.assertEqual(self.decision(workouts, queues, feedback)["explicitMissCount"], 3)
        queues[2].scheduled_date = feedback[2].scheduled_date = NOW+timedelta(days=1)
        result = self.decision(workouts, queues, feedback)
        self.assertEqual(result["explicitMissCount"], 2)
        self.assertFalse(result["interruptionReview"])
        self.assertEqual(len(athlete_reports(workouts, feedback, NOW, queues=queues)["missedWorkoutFeedback"]), 2)
        for q, f in zip(queues, feedback):
            q.scheduled_date = f.scheduled_date = NOW+timedelta(days=1)
        self.assertEqual(self.decision(workouts, queues, feedback)["explicitMissCount"], 0)
        self.assertEqual(athlete_reports(workouts, feedback, NOW, queues=queues)["missedWorkoutFeedback"], [])

    def test_duplicate_workout_feedback_cannot_manufacture_three_misses(self):
        workouts, queues, feedback = fixture()
        duplicate = Row(**vars(feedback[0]))
        result = self.decision(workouts, queues, [feedback[0], feedback[1], duplicate])
        self.assertEqual(result["explicitMissCount"], 2)
        self.assertFalse(result["interruptionReview"])
        self.assertEqual(result["volumeStatus"], "recorded_load_monitoring")
        self.assertEqual(result["gapCoverage"], "unknown")

    def test_only_matching_owned_uncompleted_running_prescriptions_count(self):
        for state in ("completed", "nonrunning", "rescheduled", "new_date", "unowned", "missing", "unknown_status"):
            with self.subTest(state=state):
                workouts, queues, feedback = fixture()
                if state == "completed":
                    queues[2].status = "completed"
                elif state == "nonrunning":
                    queues[2].activity_type = "cycling"
                elif state == "rescheduled":
                    queues[2].scheduled_date = NOW+timedelta(days=3)
                elif state == "new_date":
                    feedback[2].new_date = NOW+timedelta(days=3)
                elif state == "unowned":
                    feedback[2].user_id = "other-actor"
                elif state == "unknown_status":
                    queues[2].status = "unknown"
                else:
                    queues.pop()
                result = self.decision(workouts, queues, feedback)
                self.assertEqual(result["explicitMissCount"], 2)
                self.assertFalse(result["interruptionReview"])
                report = athlete_reports(workouts, feedback, NOW, queues=queues)
                self.assertEqual(len(report["missedWorkoutFeedback"]), 2)
                self.assertEqual(report["coverage"], "unknown")

    def test_projection_matches_skip_eligibility_and_preserves_move_adjust_reports(self):
        workouts, queues, feedback = fixture()
        queues[2].scheduled_date = feedback[2].scheduled_date = NOW+timedelta(days=1)
        duplicate = Row(**vars(feedback[0]))
        move = Row(**{**vars(feedback[0]), "action": "move"})
        adjust = Row(**{**vars(feedback[1]), "action": "adjust"})
        report = athlete_reports(workouts, [*feedback, duplicate, move, adjust], NOW, queues=queues)
        self.assertEqual([r["action"] for r in report["missedWorkoutFeedback"]].count("skip"), 2)
        self.assertEqual(len(report["missedWorkoutFeedback"]), 4)
        self.assertEqual(report["omittedFeedbackCount"], 2)
        self.assertEqual(report["coverage"], "unknown")
        self.assertEqual(report["readiness"], "unknown")
        self.assertEqual(athlete_reports(workouts, feedback, NOW)["missedWorkoutFeedback"], [])

    def test_lookback_boundary_and_non_skip_controls(self):
        for state in ("boundary", "stale", "dismissed", "move", "adjust", "unknown_reason", "naive"):
            with self.subTest(state=state):
                workouts, queues, feedback = fixture()
                if state in ("boundary", "stale"):
                    queues[2].scheduled_date = feedback[2].scheduled_date = NOW-timedelta(days=28, seconds=int(state == "stale"))
                elif state == "dismissed":
                    feedback[2].dismissed = True
                elif state in ("move", "adjust"):
                    feedback[2].action = state
                elif state == "unknown_reason":
                    feedback[2].reason = "unclassified"
                else:
                    queues[2].scheduled_date = feedback[2].scheduled_date = NOW.replace(tzinfo=None)
                    # Isolate malformed feedback from a valid queue collection;
                    # canonical aggregation requires aware prescription dates.
                    queues.pop()
                result = self.decision(workouts, queues, feedback)
                self.assertEqual(result["explicitMissCount"], 3 if state == "boundary" else 2)
                report = athlete_reports(workouts, feedback, NOW, queues=queues)
                self.assertEqual(sum(r["action"] == "skip" for r in report["missedWorkoutFeedback"]), result["explicitMissCount"])

    def test_completed_then_skipped_keeps_completion_timestamp_ineligible(self):
        workouts, queues, feedback = fixture()
        self.assertEqual(self.decision(workouts, queues, feedback)["explicitMissCount"], 3)
        # update_queue_status preserves completed_at when status later changes.
        queues[2].completed_at = NOW-timedelta(days=2)
        queues[2].status = "skipped"
        result = self.decision(workouts, queues, feedback)
        self.assertEqual(result["explicitMissCount"], 2)
        self.assertFalse(result["interruptionReview"])
        self.assertEqual(len(athlete_reports(workouts, feedback, NOW, queues=queues)["missedWorkoutFeedback"]), 2)


if __name__ == "__main__":
    unittest.main()
