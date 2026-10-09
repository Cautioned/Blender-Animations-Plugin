"""Contracts established by the Studio Animator playback integration test."""

import math
import unittest
from types import SimpleNamespace

from ..animation.easing import map_blender_to_roblox_easing
from ..animation.planning import _adaptive_curve_frames, _ROBLOX_MAPPED_INTERPOLATIONS


class TestEasingFidelity(unittest.TestCase):
    def test_native_directions_match_animator(self):
        # Studio cubic In at alpha=.25 is .015625; Out is .578125.
        for interpolation, style in (("CUBIC", "CubicV2"), ("BOUNCE", "Bounce")):
            for direction, expected in (
                ("EASE_IN", "In"),
                ("EASE_OUT", "Out"),
                ("EASE_IN_OUT", "InOut"),
            ):
                if interpolation == "BOUNCE" and direction == "EASE_IN_OUT":
                    self.assertEqual(
                        map_blender_to_roblox_easing(interpolation, direction),
                        ("Linear", "Out"),
                    )
                    continue
                with self.subTest(interpolation=interpolation, direction=direction):
                    self.assertEqual(
                        map_blender_to_roblox_easing(interpolation, direction),
                        (style, expected),
                    )

    def test_auto_uses_the_blender_curve_family_default(self):
        self.assertEqual(
            map_blender_to_roblox_easing("CUBIC", "AUTO"), ("CubicV2", "In")
        )
        self.assertEqual(
            map_blender_to_roblox_easing("BOUNCE", "AUTO"), ("Bounce", "Out")
        )

    def test_linear_and_constant_have_canonical_directions(self):
        for direction in ("AUTO", "EASE_IN", "EASE_OUT", "EASE_IN_OUT"):
            self.assertEqual(
                map_blender_to_roblox_easing("LINEAR", direction), ("Linear", "Out")
            )
            self.assertEqual(
                map_blender_to_roblox_easing("CONSTANT", direction), ("Constant", "Out")
            )

    def test_elastic_parameters_require_baking(self):
        # Blender's period/amplitude are not representable by Pose enums.
        self.assertNotIn("ELASTIC", _ROBLOX_MAPPED_INTERPOLATIONS)
        for direction in ("AUTO", "EASE_IN", "EASE_OUT", "EASE_IN_OUT"):
            self.assertEqual(
                map_blender_to_roblox_easing("ELASTIC", direction), ("Linear", "Out")
            )

    def test_adaptive_samples_preserve_overshoot_and_sharp_curves(self):
        for evaluate in (
            lambda x: math.sqrt(max(0, 1 - (x - 1) ** 2)),
            lambda x: math.sin(x * math.pi * 8) * math.exp(-x * 3),
        ):
            with self.subTest(curve=evaluate):
                curve = SimpleNamespace(evaluate=evaluate)
                frames = sorted({0.0, 1.0} | _adaptive_curve_frames(curve, 0, 1))
                self.assertGreater(len(frames), 2)
                for a, b in zip(frames, frames[1:]):
                    for fraction in (0.25, 0.5, 0.75):
                        expected = evaluate(a + (b - a) * fraction)
                        linear = evaluate(a) + (evaluate(b) - evaluate(a)) * fraction
                        self.assertLess(abs(expected - linear), 0.002)

    def test_linear_curves_need_no_extra_samples(self):
        self.assertFalse(
            _adaptive_curve_frames(SimpleNamespace(evaluate=lambda x: x * 4 + 2), 0, 20)
        )
