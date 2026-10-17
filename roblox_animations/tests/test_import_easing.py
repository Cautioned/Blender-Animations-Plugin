"""Regression coverage for issue #69's measured Animator easing behavior."""

import math
import unittest

import bpy
from mathutils import Matrix, Quaternion, Vector

from ..animation.import_easing import pose_easing_value, sample_import_keyframes
from ..core.utils import cf_to_mat, get_action_fcurves, mat_to_cf
from ..server import requests
from ..rig.creation import create_joint_bone
from ..core.constants import get_transform_to_blender
from ..animation.sampling import serialize_animation_state
from .test_studio_template_roundtrip import templates


class TestImportEasing(unittest.TestCase):
    def test_measured_animator_fractions(self):
        # Independent values reported from Animator:StepAnimations in issue #69.
        rows = {
            ("Linear", "In"): (.200, .500, .800),
            ("Constant", "In"): (1, 1, 1),
            ("Constant", "Out"): (0, 0, 0),
            ("Constant", "InOut"): (0, 1, 1),
            ("Cubic", "In"): (.488, .875, .992),
            ("Cubic", "Out"): (.008, .125, .512),
            ("Cubic", "InOut"): (.032, .500, .968),
            ("CubicV2", "In"): (.008, .125, .512),
            ("CubicV2", "Out"): (.488, .875, .992),
            ("CubicV2", "InOut"): (.032, .500, .968),
            ("Bounce", "In"): (.060, .234, .698),
            ("Bounce", "Out"): (.303, .766, .940),
            ("Bounce", "InOut"): (.114, .500, .545),
            ("Elastic", "In"): (-.002, -.016, -.125),
            ("Elastic", "Out"): (1.125, 1.016, 1.002),
            ("Elastic", "InOut"): (-.004, .500, 1.004),
        }
        for (style, direction), expected in rows.items():
            for alpha, value in zip((.2, .5, .8), expected):
                with self.subTest(style=style, direction=direction, alpha=alpha):
                    self.assertAlmostEqual(
                        pose_easing_value(style, direction, alpha), value, delta=.00051,
                    )

    def test_sparse_tracks_and_fractional_hold_boundaries(self):
        identity = mat_to_cf(Matrix.Identity(4))
        moved = mat_to_cf(Matrix.Translation((4, 0, 0)))
        keys = [
            {"t": .25 / 30, "kf": {"A": [identity, "Constant", "InOut"]}},
            {"t": 2 / 30, "kf": {"B": identity}},
            {"t": 5.5 / 30, "kf": {"A": {"components": moved}}},
            {"t": 7 / 30, "kf": {"B": moved}},
        ]
        sampled = sample_import_keyframes(keys, 30)
        by_frame = {round(k["t"] * 30, 6): k["kf"] for k in sampled}
        self.assertNotIn("B", by_frame[.25])
        self.assertEqual(cf_to_mat(by_frame[2]["A"][0]).translation.x, 0)
        self.assertEqual(cf_to_mat(by_frame[2.875]["A"][0]).translation.x, 4)
        self.assertEqual(cf_to_mat(by_frame[7]["A"][0]).translation.x, 4)
        self.assertIn(5.5, by_frame)

    def test_actual_import_matches_pose_frames_and_constant_edges(self):
        # Exercise the real request handler, CFrame conversion and F-curve evaluation.
        if bpy.context.object and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.add(type="ARMATURE", enter_editmode=True)
        rig = bpy.context.object
        bone = rig.data.edit_bones.new("EasingBone")
        bone.head, bone.tail = (0, 0, 0), (0, 1, 0)
        bone.use_deform = True
        bone["rbx_is_deform_bone"] = True
        bpy.ops.object.mode_set(mode="POSE")
        rig.pose.bones["EasingBone"].rotation_mode = "XYZ"
        axis = Vector((1, 2, -3)).normalized()
        swizzle = Matrix.Diagonal((-1, 1, -1, 1))
        start, end = .25, 30.75
        target = Matrix.LocRotScale(Vector((10, -2, 3)), Quaternion(axis, 2.6), Vector((1, 1, 1)))
        try:
            for style in ("Linear", "Cubic", "CubicV2", "Bounce", "Elastic", "Constant"):
                for direction in ("In", "Out", "InOut"):
                    with self.subTest(style=style, direction=direction):
                        data = {
                            "t": end / 30, "is_deform_bone_rig": True,
                            "export_info": {"fps": 30, "deform_translation_scale_factor": 1},
                            "kfs": [
                                {"t": start / 30, "kf": {"EasingBone": [mat_to_cf(Matrix.Identity(4)), style, direction]}},
                                {"t": end / 30, "kf": {"EasingBone": mat_to_cf(target)}},
                            ],
                        }
                        requests.execute_import_animation("easing-import-test", data, rig.name)
                        success, message = requests.pending_responses.pop("easing-import-test")
                        self.assertTrue(success, message)
                        self.assertEqual(bpy.context.scene.frame_end, 31)
                        frames = [start, end] + list(range(1, 31))
                        if style == "Constant":
                            edge = start if direction == "In" else (start + end) / 2 if direction == "InOut" else end
                            frames += [max(start, edge - .001), min(end, edge + .001)]
                        for frame in frames:
                            bpy.context.scene.frame_set(math.floor(frame), subframe=frame % 1)
                            factor = pose_easing_value(style, direction, (frame - start) / (end - start))
                            expected = swizzle @ Matrix.LocRotScale(
                                Vector((10, -2, 3)) * factor, Quaternion(axis, 2.6 * factor), Vector((1, 1, 1)),
                            ) @ swizzle
                            actual = rig.pose.bones["EasingBone"].matrix_basis
                            for row in range(4):
                                for column in range(4):
                                    self.assertAlmostEqual(actual[row][column], expected[row][column], delta=2e-5, msg=f"frame={frame} row={row} col={column}")
                        # Imported samples must not inherit Blender's Bezier defaults.
                        for curve in get_action_fcurves(rig.animation_data.action):
                            self.assertTrue(all(k.interpolation in {"LINEAR", "CONSTANT"} for k in curve.keyframe_points))
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")
            bpy.data.objects.remove(rig, do_unlink=True)

    def test_motor6d_parent_and_staggered_child_tracks(self):
        for name, fixture in templates().items():
            with self.subTest(rig=name):
                if bpy.context.object and bpy.context.object.mode != "OBJECT":
                    bpy.ops.object.mode_set(mode="OBJECT")
                bpy.ops.object.add(type="ARMATURE", enter_editmode=True)
                rig = bpy.context.object
                root = rig.data.edit_bones.new("HumanoidRootPart")
                root.matrix = get_transform_to_blender() @ cf_to_mat(fixture["parts"][root.name])
                root.length = .3
                for key, matrix in (
                    ("transform", cf_to_mat(fixture["parts"][root.name])),
                    ("transform1", Matrix.Identity(4)),
                    ("nicetransform", Matrix.Identity(4)),
                ):
                    root[key] = [list(row) for row in matrix]
                bpy.ops.object.mode_set(mode="OBJECT")
                pending = list(fixture["joints"])
                while pending:
                    ready = [joint for joint in pending if joint["part0"] in rig.data.bones]
                    self.assertTrue(ready)
                    for joint in ready:
                        create_joint_bone(rig, joint["part0"], fixture["parts"][joint["part1"]], joint["c0"], joint["c1"], joint["part1"])
                        pending.remove(joint)
                parent = "Torso" if name == "TemplateR6" else "LowerTorso"
                child = "Right Arm" if name == "TemplateR6" else "RightHand"
                axis = Vector((1, 2, -3)).normalized()
                identity = mat_to_cf(Matrix.Identity(4))
                try:
                    for style in ("Cubic", "CubicV2", "Bounce", "Elastic", "Constant"):
                        for direction in ("In", "Out", "InOut"):
                            target = Matrix.LocRotScale(Vector((.5, -.2, .3)), Quaternion(axis, 1.7), Vector((1, 1, 1)))
                            data = {"t": 1, "export_info": {"fps": 30}, "kfs": [
                                {"t": 0, "kf": {parent: [identity, style, direction]}},
                                {"t": 5 / 30, "kf": {child: [identity, style, direction]}},
                                {"t": 25 / 30, "kf": {child: mat_to_cf(target)}},
                                {"t": 1, "kf": {parent: mat_to_cf(target)}},
                            ]}
                            requests.execute_import_animation("motor-easing-test", data, rig.name)
                            success, message = requests.pending_responses.pop("motor-easing-test")
                            self.assertTrue(success, message)
                            for frame in range(31):
                                bpy.context.scene.frame_set(frame)
                                actual = serialize_animation_state(rig)
                                for bone, alpha in ((parent, frame / 30), (child, (frame - 5) / 20)):
                                    factor = pose_easing_value(style, direction, alpha)
                                    expected = Matrix.LocRotScale(Vector((.5, -.2, .3)) * factor, Quaternion(axis, 1.7 * factor), Vector((1, 1, 1)))
                                    components = actual.get(bone, identity)
                                    error = max(abs(a - b) for a, b in zip(components, mat_to_cf(expected)))
                                    # Motor6D basis inversions accumulate float32 error along R15 chains.
                                    self.assertLess(error, 1e-4, (name, style, direction, frame, bone))
                finally:
                    bpy.ops.object.mode_set(mode="OBJECT")
                    bpy.data.objects.remove(rig, do_unlink=True)
