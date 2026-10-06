import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from calibration import (  # noqa: E402
    GAIN_FACTOR_MAX,
    GAIN_FACTOR_MIN,
    MIN_CONDITION_SUPPORT,
    OnlineOutcomeCalibrator,
    ScienceGainCalibrator,
)


START = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)
POSITIVE_FEATURES = {"direction": "N", "program": "DARK"}
GAIN_FEATURES = {
    "direction": "N",
    "program": "DARK",
    "exposure_seconds": 900,
    "assignments_count": 2,
}


def stamp(hours):
    return START + timedelta(hours=hours)


def freeze_positive(model, index, label=None, *, features=None, base=None, epoch=None):
    issue = stamp(index * 2)
    row = model.freeze(
        index, base, features or POSITIVE_FEATURES, issue, issue + timedelta(hours=1),
        input_hash=f"input-{index}", epoch=epoch,
    )
    if label is not None:
        assert model.label(index, label, issue + timedelta(hours=1))
    return row


def freeze_gain(model, index, *, score=0.8, base=0.5, features=None):
    issue = stamp(index * 2)
    row = model.freeze(
        index,
        base,
        features or GAIN_FEATURES,
        {"target": 0.0},
        ["target"],
        issue,
        issue + timedelta(hours=1),
        input_hash=f"gain-{index}",
    )
    result = {
        "action": "observe",
        "observe_index": index,
        "assigned_count": 1,
        "hit_count": 1,
        "hits": [{"target_id": "target", "score": score}],
    }
    return row, result, issue + timedelta(hours=1)


class OnlineOutcomeCalibratorTests(unittest.TestCase):
    def test_incomplete_immature_and_duplicate_labels_do_not_update(self):
        model = OnlineOutcomeCalibrator()
        issue = stamp(0)
        frozen = model.freeze(
            0, None, POSITIVE_FEATURES, issue, issue + timedelta(hours=1), input_hash="h0"
        )
        self.assertEqual(frozen["base_hit_fraction"], 0.5)
        self.assertFalse(model.label(99, 0.7, stamp(1)))
        self.assertFalse(model.label(0, None, stamp(1)))
        self.assertFalse(model.label(0, 0.7, stamp(1), public=False))
        self.assertFalse(model.label(0, float("nan"), stamp(1)))
        self.assertFalse(model.label(0, 0.7, stamp(0.99)))
        self.assertEqual(model.summary()["labeled_exposure_count"], 0)
        self.assertTrue(model.label(0, 0.75, stamp(1)))
        self.assertFalse(model.label(0, 0.8, stamp(1.5)))
        summary = model.summary()
        self.assertEqual(summary["sum_labels"], 0.75)
        self.assertEqual(summary["labeled_exposure_count"], 1)
        self.assertFalse(summary["action_gate_enabled"])

    def test_same_timestamp_label_is_replayed_before_next_action_freeze(self):
        model = OnlineOutcomeCalibrator()
        start = stamp(0)
        end = stamp(1)
        model.freeze(0, None, POSITIVE_FEATURES, start, end, input_hash="first")
        self.assertTrue(model.label(0, 1.0, end))

        second = model.freeze(
            1, None, POSITIVE_FEATURES, end, stamp(2), input_hash="second"
        )
        self.assertAlmostEqual(second["base_hit_fraction"], 2.0 / 3.0)

    def test_beta_base_is_same_epoch_and_condition_needs_eight_labels(self):
        model = OnlineOutcomeCalibrator()
        for index in range(7):
            freeze_positive(model, index, 0.9, epoch="night-a")
        for index in range(7, 14):
            freeze_positive(
                model,
                index,
                0.1,
                features={"direction": "S", "program": "BRIGHT"},
                epoch="night-a",
            )

        base = model.predict(None, POSITIVE_FEATURES, epoch="night-a")
        self.assertAlmostEqual(base, 0.5)
        self.assertEqual(model.predict(0.37, POSITIVE_FEATURES, epoch="night-a"), 0.37)
        self.assertEqual(
            model.predict(None, {"direction": "X", "program": "DARK"}, epoch="night-a"),
            base,
        )
        self.assertEqual(model.predict(None, POSITIVE_FEATURES, epoch="night-b"), 0.5)

        freeze_positive(model, 14, 0.9, epoch="night-a")
        self.assertGreater(model.predict(None, POSITIVE_FEATURES, epoch="night-a"), base)
        self.assertAlmostEqual(model.summary(epoch="night-a")["sum_labels"], 7.9)
        self.assertEqual(model.summary(epoch="night-a")["labeled_exposure_count"], 15)
        self.assertFalse(model.action_gate_enabled)

    def test_retraction_replays_predictions_and_metrics(self):
        model = OnlineOutcomeCalibrator()
        for index in range(12):
            freeze_positive(model, index, 0.2, base=0.35)

        self.assertEqual(model.retract_window(0, 4), 4)
        reference = OnlineOutcomeCalibrator()
        for index in range(4, 12):
            freeze_positive(reference, index, 0.2, base=0.35)
        self.assertEqual(model.summary(), reference.summary())
        self.assertAlmostEqual(
            model.predict(0.35, POSITIVE_FEATURES),
            reference.predict(0.35, POSITIVE_FEATURES),
        )

    def test_history_is_bounded_and_reset_starts_a_new_epoch(self):
        model = OnlineOutcomeCalibrator(max_records=4)
        for index in range(6):
            freeze_positive(model, index, 0.4)
        self.assertEqual(model.summary()["record_count"], 4)
        self.assertFalse(model.label(0, 0.4, stamp(1)))
        model.reset()
        self.assertEqual(model.summary()["record_count"], 0)
        frozen = model.freeze(0, None, POSITIVE_FEATURES, stamp(20), stamp(21))
        self.assertEqual(frozen["epoch"], 1)


class ScienceGainCalibratorTests(unittest.TestCase):
    def test_public_label_uses_frozen_assigned_targets_and_misses_are_zero(self):
        model = ScienceGainCalibrator()
        issue = stamp(0)
        model.freeze(
            0, 0.5, GAIN_FEATURES, {"a": 0.2, "b": 0.4}, ["a", "b"],
            issue, issue + timedelta(hours=1), input_hash="input-gain",
        )
        self.assertFalse(model.label(0, {"action": "observe", "observe_index": 0}, stamp(1)))
        self.assertFalse(model.label(0, {
            "action": "observe", "observe_index": 0, "assigned_count": 2,
            "hit_count": 1, "hits": [{"target_id": "a", "score": 0.5}],
        }, stamp(0.9)))
        result = {
            "action": "observe",
            "observe_index": 0,
            "assigned_count": 2,
            "hit_count": 1,
            "hits": [{"target_id": "a", "score": 0.5}],
        }
        self.assertTrue(model.label(0, result, stamp(1)))
        self.assertFalse(model.label(0, result, stamp(1.5)))
        summary = model.summary()
        self.assertEqual(summary["labeled_exposure_count"], 1)
        self.assertAlmostEqual(summary["sum_labels"], 0.3)
        self.assertTrue(summary["effect_unverified"])
        self.assertIn("no unselected or counterfactual effects", summary["policy_support"])
        self.assertNotIn("records", summary)
        self.assertEqual(len(model.records()), 1)
        self.assertEqual(len(model.summary(include_records=True)["records"]), 1)

    def test_weighted_hit_score_above_one_is_accepted(self):
        model = ScienceGainCalibrator()
        issue = stamp(0)
        model.freeze(
            0, 0.5, GAIN_FEATURES, {"target": 0.75}, ["target"],
            issue, stamp(1), input_hash="weighted-score",
        )
        result = {
            "action": "observe",
            "observe_index": 0,
            "assigned_count": 1,
            "hit_count": 1,
            "hits": [{"target_id": "target", "score": 1.25}],
        }

        self.assertTrue(model.label(0, result, stamp(1)))
        self.assertAlmostEqual(model.summary()["sum_labels"], 0.5)

    def test_gain_gate_requires_support_and_prequential_improvement(self):
        model = ScienceGainCalibrator()
        for index in range(MIN_CONDITION_SUPPORT - 1):
            _row, result, received = freeze_gain(model, index)
            self.assertTrue(model.label(index, result, received))
        self.assertEqual(model.predict_gain(0.5, GAIN_FEATURES), 0.5)
        self.assertFalse(model.action_gate_enabled)

        _row, result, received = freeze_gain(model, MIN_CONDITION_SUPPORT - 1)
        self.assertTrue(model.label(MIN_CONDITION_SUPPORT - 1, result, received))
        corrected = model.predict_gain(0.5, GAIN_FEATURES)
        self.assertGreater(corrected, 0.5)
        self.assertLessEqual(corrected, 0.5 * GAIN_FACTOR_MAX)
        self.assertGreaterEqual(corrected / 0.5, GAIN_FACTOR_MIN)
        self.assertTrue(model.action_gate_enabled)
        self.assertEqual(model.predict_gain(0.5, {**GAIN_FEATURES, "direction": "X"}), 0.5)
        self.assertFalse(model.action_gate_enabled)

    def test_no_prequential_gain_improvement_keeps_factor_at_one(self):
        model = ScienceGainCalibrator()
        for index in range(MIN_CONDITION_SUPPORT):
            _row, result, received = freeze_gain(model, index, score=0.5)
            self.assertTrue(model.label(index, result, received))
        self.assertEqual(model.predict_gain(0.5, GAIN_FEATURES), 0.5)
        self.assertFalse(model.action_gate_enabled)

    def test_read_only_prediction_does_not_update_last_gate_diagnostic(self):
        model = ScienceGainCalibrator()
        for index in range(MIN_CONDITION_SUPPORT):
            _row, result, received = freeze_gain(model, index)
            self.assertTrue(model.label(index, result, received))

        self.assertFalse(model.summary()["last_prediction_gate_enabled"])
        prediction = model.predict_gain(0.5, GAIN_FEATURES, record_gate=False)
        self.assertGreater(prediction, 0.5)
        self.assertTrue(model.summary()["action_gate_enabled"])
        self.assertFalse(model.summary()["last_prediction_gate_enabled"])

        model.predict_gain(0.5, GAIN_FEATURES)
        self.assertTrue(model.summary()["last_prediction_gate_enabled"])
        model.predict_gain(0.5, GAIN_FEATURES, record_gate=False)
        self.assertTrue(model.summary()["last_prediction_gate_enabled"])

    def test_science_gain_retraction_replays_gate_and_predictions(self):
        model = ScienceGainCalibrator()
        for index in range(12):
            _row, result, received = freeze_gain(model, index)
            self.assertTrue(model.label(index, result, received))
        self.assertTrue(model.summary()["action_gate_enabled"])
        self.assertEqual(model.retract_window(0, 4), 4)

        reference = ScienceGainCalibrator()
        for index in range(4, 12):
            _row, result, received = freeze_gain(reference, index)
            self.assertTrue(reference.label(index, result, received))
        self.assertEqual(model.summary(), reference.summary())
        self.assertAlmostEqual(
            model.predict_gain(0.5, GAIN_FEATURES),
            reference.predict_gain(0.5, GAIN_FEATURES),
        )

    def test_same_timestamp_label_is_replayed_before_next_action_freeze(self):
        model = ScienceGainCalibrator(min_condition_support=2)
        for index in range(2):
            _row, result, received = freeze_gain(model, index)
            self.assertTrue(model.label(index, result, received))
        self.assertTrue(model.summary()["action_gate_enabled"])

        third = model.freeze(
            2, 0.5, GAIN_FEATURES, {"target": 0.0}, ["target"],
            stamp(3), stamp(4), input_hash="same-time-third",
        )
        self.assertTrue(third["action_gate_enabled_at_issue"])
        self.assertGreater(third["predicted_gain"], third["base_gain"])


if __name__ == "__main__":
    unittest.main()
