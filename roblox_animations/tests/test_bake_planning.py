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
