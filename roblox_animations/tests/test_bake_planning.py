import unittest
from types import SimpleNamespace

from ..animation.planning import build_bake_plan


class CountingKeys(list):
    def __init__(self, keys):
        super().__init__(keys)
        self.visits = 0

    def __iter__(self):
        for key in super().__iter__():
            self.visits += 1
            yield key


def curve(keys):
    return SimpleNamespace(
        data_path='pose.bones["Root"].location',
        modifiers=[],
        keyframe_points=CountingKeys([
            SimpleNamespace(co=SimpleNamespace(x=frame), interpolation=style, easing="AUTO")
            for frame, style in keys
        ]),
    )


class TestBakePlanning(unittest.TestCase):
    def plan(self, curves, end):
        return build_bake_plan(
            SimpleNamespace(pose=SimpleNamespace(bones=[])), set(), object(), {},
            curves, {1, end}, 1, end, 30, False, False, 1,
        )

    def test_staggered_channel_style_changes(self):
        curves = [
            curve([(1, "CONSTANT"), (5, "LINEAR"), (9, "LINEAR")]),
            curve([(2, "LINEAR"), (7, "CONSTANT"), (9, "CONSTANT")]),
        ]
        plan = self.plan(curves, 9)
        self.assertEqual(plan.mixed_interpolation_segments["Root"], {(2, 5), (7, 9)})

    def test_baked_channels_do_not_rescan_all_previous_keys(self):
        count = 4000
        curves = [curve([(frame, "LINEAR") for frame in range(1, count + 1)]) for _ in range(10)]
        plan = self.plan(curves, count)
        self.assertFalse(plan.mixed_interpolation_segments)
        self.assertLess(sum(c.keyframe_points.visits for c in curves), count * len(curves) * 12)

    def test_fractional_keys_keep_times_without_refining_frame_wide_motion(self):
        control = curve([(1, "LINEAR"), (1.25, "BEZIER"), (3, "BEZIER")])
        def unexpected_evaluate(frame):
            self.fail("Visually refitted input was adaptively sampled twice")
        control.evaluate = unexpected_evaluate
        plan = build_bake_plan(
            SimpleNamespace(pose=SimpleNamespace(bones=[])), set(), object(), {},
            [control], {1, 3}, 1, 3, 30, False, True, 1,
        )
        self.assertEqual(plan.frames, [1, 1.25, 2, 3])
        self.assertIn(1.25, plan.keyframe_times)

    def test_subframe_curve_receives_automatic_samples(self):
        direct = curve([(1, "BEZIER"), (1.75, "BEZIER")])
        direct.evaluate = lambda frame: (frame - 1) ** 2
        plan = build_bake_plan(
            SimpleNamespace(pose=SimpleNamespace(bones=[])), set(), object(), {},
            [direct], {1, 2}, 1, 2, 30, False, False, 1,
        )
        self.assertGreater(len(plan.frames), 3)
        self.assertIn(1.75, plan.frames)

    def test_whole_frame_curves_do_not_trigger_recursive_sampling(self):
        direct = curve([(1, "BEZIER"), (278, "BEZIER")])
        def unexpected_evaluate(frame):
            self.fail("Whole-frame animation triggered extra curve fitting")
        direct.evaluate = unexpected_evaluate
        plan = self.plan([direct], 278)
        self.assertTrue(all(frame == int(frame) for frame in plan.frames))

    def test_fractional_constant_keys_preserve_holds_without_refinement(self):
        stepped = curve([(1, "CONSTANT"), (1.25, "CONSTANT"), (2, "CONSTANT")])
        stepped.evaluate = lambda frame: self.fail("Constant segment was refined")
        plan = self.plan([stepped], 2)
        self.assertIn(1.25, plan.frames)
