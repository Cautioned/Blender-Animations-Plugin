"""Fit evaluated motion, including drivers, holds, and nonlinear constraints."""
import math
import unittest

from ..animation.ir import KeyframePayload, PoseEntry
from ..animation.visual_bake import refine_visual_bake


def cf(x):
    return [x, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]


class TestVisualBake(unittest.TestCase):
    def bake(self, function, end=2):
        # Misleading CONSTANT metadata on the output must not freeze a driver.
        rows = [KeyframePayload(t / 30, {"Bone": PoseEntry(cf(function(t)), "Constant", "Out")})
                for t in (0, end)]
        return refine_visual_bake(rows, {"Bone"}, lambda t: {"Bone": cf(function(t))}, 0, end, 30)

    def value(self, rows, frame):
        for a, b in zip(rows, rows[1:]):
            if a.time * 30 <= frame < b.time * 30:
                start, end = a.poses["Bone"], b.poses["Bone"]
                if start.style == "Constant":
                    return start.components[0]
                alpha = (frame / 30 - a.time) / (b.time - a.time)
                return start.components[0] * (1 - alpha) + end.components[0] * alpha
        return rows[-1].poses["Bone"].components[0]

    def test_driver_controlled_step_has_no_linear_ramp(self):
        rows = self.bake(lambda t: 0 if t < 1 else 3)
        self.assertEqual(rows[0].poses["Bone"].style, "Constant")
        self.assertEqual(self.value(rows, 0.99999), 0)
        self.assertEqual(self.value(rows, 1), 3)

    def test_continuous_driver_overrides_constant_input_metadata(self):
        rows = self.bake(lambda t: t * 2)
        self.assertTrue(all(row.poses["Bone"].style == "Linear" for row in rows))
        self.assertAlmostEqual(self.value(rows, 0.73), 1.46)

    def test_nonlinear_output_refines_between_integer_frames(self):
        def function(t):
            return math.sin(t * 3) * 0.5
        rows = self.bake(function)
        self.assertGreater(len(rows), 3)
        for i in range(401):
            frame = i / 200
            self.assertLess(abs(self.value(rows, frame) - function(frame)), 0.0006)

    def test_subframe_discontinuity_preserves_hold(self):
        rows = self.bake(lambda t: 0 if t < 0.37 else 2)
        self.assertEqual(self.value(rows, 0.369), 0)
        self.assertEqual(self.value(rows, 0.371), 2)

    def test_other_bones_and_face_metadata_survive(self):
        other = PoseEntry(cf(7), "Bounce", "In")
        face = {"Smile": {"value": 0.4}}
        original = KeyframePayload(0, {"Other": other}, face)
        rows = refine_visual_bake([original], {"Bone"}, lambda t: {"Bone": cf(t)}, 0, 2, 30)
        self.assertIs(rows[0].poses["Other"], other)
        self.assertEqual(rows[0].face, face)

    def test_missing_identity_samples_still_emit_the_bone(self):
        rows = [KeyframePayload(0, {"Bone": PoseEntry(cf(0), "Linear", "Out")})]
        baked = refine_visual_bake(rows, {"Bone"}, lambda t: {} if t < 1 else {"Bone": cf(2)}, 0, 2, 30)
        self.assertEqual(self.value(baked, 0.9), 0)
        self.assertEqual(self.value(baked, 1.1), 2)
        self.assertTrue(all("Bone" in row.poses for row in baked))

    def test_pathological_motion_has_a_bounded_sampling_cost(self):
        calls = []
        names = {f"Bone{i}" for i in range(200)}
        def sample(frame):
            calls.append(frame)
            return {name: cf(math.sin(frame * 100000)) for name in names}
        rows = [KeyframePayload(0, {name: PoseEntry(cf(0), "Linear", "Out") for name in names})]
        diagnostics = {}
        refine_visual_bake(rows, names, sample, 0, 1, 30, diagnostics)
        self.assertLess(len(calls), 64)
        self.assertGreater(diagnostics["limited_segments"], 0)

    def test_reuses_raw_samples_without_repeating_evaluation(self):
        seeds = {frame: {"Bone": cf(frame)} for frame in (0, 0.25, 0.5, 0.75, 1)}
        calls = []
        def sample(frame):
            calls.append(frame)
            return {"Bone": cf(frame)}
        rows = [KeyframePayload(0, {"Bone": PoseEntry(cf(0), "Linear", "Out")})]
        baked = refine_visual_bake(rows, {"Bone"}, sample, 0, 1, 30, seed_samples=seeds)
        self.assertFalse(set(calls) & set(seeds))
        self.assertAlmostEqual(self.value(baked, 0.37), 0.37)

    def test_refit_retains_fractional_source_steps_and_other_channels(self):
        rows = [KeyframePayload(t / 30, {
            "Bone": PoseEntry(cf(0 if t < 0.37 else 2), "Constant", "Out"),
            "Other": PoseEntry(cf(t), "Linear", "Out"),
        }) for t in (0, 0.125, 0.37, 0.75, 1)]
        baked = refine_visual_bake(rows, {"Bone"},
                                  lambda t: {"Bone": cf(0 if t < 0.37 else 2)},
                                  0, 1, 30, keyframes={0, 0.37, 1})
        other_times = [round(row.time * 30, 6) for row in baked if "Other" in row.poses]
        self.assertEqual(other_times, [0, 0.125, 0.37, 0.75, 1])
        channel = [row for row in baked if "Bone" in row.poses]
        self.assertEqual(self.value(channel, 0.369), 0)
        self.assertEqual(self.value(channel, 0.371), 2)
